"""Helper для чтения секретов alla из secman через hvac."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import hvac
from alla.exceptions import ConfigurationError

ALLURE_SECRET_KEYS = (
    "ALLURE_TOKEN",
    "ALLURE_KB_POSTGRES_DSN",
    "ALLURE_LANGFLOW_API_KEY",
)
DEFAULT_K8S_JWT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
DEFAULT_K8S_AUTH_MOUNT_POINT = "kubernetes"
DEFAULT_KV_VERSION = "v2"


def _require_env(name: str) -> str:
    """Прочитать обязательную env-переменную."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set for secman access")
    return value


def _read_jwt_token(path_str: str) -> str:
    """Прочитать service account JWT из файла."""
    jwt_path = Path(path_str)
    try:
        token = jwt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Kubernetes JWT file not found: {jwt_path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"Failed to read Kubernetes JWT file: {jwt_path}") from exc

    if not token:
        raise ConfigurationError(f"Kubernetes JWT file is empty: {jwt_path}")

    return token


def _mask_secret(value: str) -> str:
    """Вернуть безопасное представление секрета для demo-вывода."""
    return f"<hidden:{len(value)} chars>"


def build_secman_client() -> hvac.Client:
    """Создать hvac-клиент для secman."""
    addr = _require_env("SECMAN_ADDR")
    namespace = os.environ.get("SECMAN_NAMESPACE", "").strip()

    client_kwargs: dict[str, str] = {"url": addr}
    if namespace:
        client_kwargs["namespace"] = namespace

    return hvac.Client(**client_kwargs)


def login_with_kubernetes(client: hvac.Client) -> None:
    """Аутентифицироваться в secman через Kubernetes JWT."""
    role = _require_env("SECMAN_K8S_ROLE")
    jwt_path = os.environ.get("SECMAN_K8S_JWT_PATH", DEFAULT_K8S_JWT_PATH).strip()
    if not jwt_path:
        jwt_path = DEFAULT_K8S_JWT_PATH

    jwt = _read_jwt_token(jwt_path)

    client.auth.kubernetes.login(
        role=role,
        jwt=jwt,
        mount_point=DEFAULT_K8S_AUTH_MOUNT_POINT,
    )


def fetch_allure_secrets() -> dict[str, str]:
    """Прочитать секреты alla из secman."""
    kv_version = os.environ.get("SECMAN_KV_VERSION", DEFAULT_KV_VERSION).strip().lower()
    if not kv_version:
        kv_version = DEFAULT_KV_VERSION
    if kv_version != DEFAULT_KV_VERSION:
        raise ConfigurationError(f"Unsupported SECMAN_KV_VERSION: {kv_version!r}")

    mount_point = _require_env("SECMAN_MOUNT_POINT")
    secret_path = _require_env("SECMAN_SECRET_PATH")

    client = build_secman_client()
    login_with_kubernetes(client)

    response = client.secrets.kv.v2.read_secret_version(
        path=secret_path,
        mount_point=mount_point,
    )

    if not isinstance(response, dict):
        raise ConfigurationError("Secman response does not contain KV v2 secret data")

    response_data = response.get("data")
    if not isinstance(response_data, dict):
        raise ConfigurationError("Secman response does not contain KV v2 secret data")

    secret_data = response_data.get("data")
    if not isinstance(secret_data, dict):
        raise ConfigurationError("Secman response does not contain KV v2 secret data")

    missing_keys = [key for key in ALLURE_SECRET_KEYS if key not in secret_data]
    if missing_keys:
        missing = ", ".join(missing_keys)
        raise ConfigurationError(f"Secman secret is missing required keys: {missing}")

    secrets: dict[str, str] = {}
    for key in ALLURE_SECRET_KEYS:
        value = secret_data[key]
        if not isinstance(value, str):
            raise ConfigurationError(f"Secman secret {key} must be a string")
        secrets[key] = value
    return secrets


def main() -> int:
    """Demo entrypoint для локальной проверки helper-а."""
    try:
        secrets = fetch_allure_secrets()
    except ConfigurationError as exc:
        print(f"secman helper error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("secman helper interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"secman helper failed: {exc}", file=sys.stderr)
        return 1

    print("Fetched secrets from secman:")
    for key in ALLURE_SECRET_KEYS:
        print(f"{key}={_mask_secret(secrets[key])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
