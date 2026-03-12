"""Клиент для чтения секретов alla из secman через hvac."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import hvac

from alla.clients.base import SecmanProvider
from alla.config import Settings
from alla.exceptions import ConfigurationError

ALLURE_SECRET_KEYS = (
    "ALLURE_TOKEN",
    "ALLURE_KB_POSTGRES_DSN",
    "ALLURE_LANGFLOW_API_KEY",
)
DEFAULT_K8S_JWT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
DEFAULT_K8S_AUTH_MOUNT_POINT = "kubernetes"
DEFAULT_KV_VERSION = "v2"


class SecmanClient(SecmanProvider):
    """Синхронный hvac-клиент для чтения секретов из secman."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def _require_setting(env_name: str, value: str) -> str:
        """Проверить, что обязательная настройка задана."""
        value = value.strip()
        if not value:
            raise ConfigurationError(f"{env_name} must be set for secman access")
        return value

    @staticmethod
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

    @staticmethod
    def _mask_secret(value: str) -> str:
        """Вернуть безопасное представление секрета для demo-вывода."""
        return f"<hidden:{len(value)} chars>"

    @staticmethod
    def _parse_ssl_verify(raw: str) -> bool | str:
        """Преобразовать строковую настройку SSL verify в значение для hvac.

        Допустимые значения:
        - ``"true"``  → ``True``  (проверять SSL, системный CA bundle)
        - ``"false"`` → ``False`` (не проверять SSL — **только для отладки!**)
        - путь к файлу → строка   (кастомный CA bundle, например
          ``/etc/ssl/certs/ca-bundle.crt``)
        """
        value = raw.strip()
        if value.lower() == "true" or value == "":
            return True
        if value.lower() == "false":
            return False
        # Всё остальное трактуем как путь к CA bundle
        return value

    def build_secman_client(self) -> hvac.Client:
        """Создать hvac-клиент для secman."""
        addr = self._require_setting("ALLURE_SECMAN_ADDR", self._settings.secman_addr)
        namespace = self._settings.secman_namespace.strip()
        verify = self._parse_ssl_verify(self._settings.secman_ssl_verify)

        client_kwargs: dict = {"url": addr, "verify": verify}
        if namespace:
            client_kwargs["namespace"] = namespace

        return hvac.Client(**client_kwargs)

    def login_with_kubernetes(self, client: hvac.Client) -> None:
        """Аутентифицироваться в secman через Kubernetes JWT."""
        role = self._require_setting("ALLURE_SECMAN_K8S_ROLE", self._settings.secman_k8s_role)
        jwt_path = self._settings.secman_k8s_jwt_path.strip() or DEFAULT_K8S_JWT_PATH
        jwt = self._read_jwt_token(jwt_path)

        client.auth.kubernetes.login(
            role=role,
            jwt=jwt,
            mount_point=DEFAULT_K8S_AUTH_MOUNT_POINT,
        )

    def fetch_allure_secrets(self) -> dict[str, str]:
        """Прочитать секреты alla из secman."""
        kv_version = self._settings.secman_kv_version.strip().lower() or DEFAULT_KV_VERSION
        if kv_version != DEFAULT_KV_VERSION:
            raise ConfigurationError(f"Unsupported ALLURE_SECMAN_KV_VERSION: {kv_version!r}")

        mount_point = self._require_setting(
            "ALLURE_SECMAN_MOUNT_POINT",
            self._settings.secman_mount_point,
        )
        secret_path = self._require_setting(
            "ALLURE_SECMAN_SECRET_PATH",
            self._settings.secman_secret_path,
        )

        client = self.build_secman_client()
        self.login_with_kubernetes(client)

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


def _read_demo_env(name: str, default: str = "") -> str:
    """Прочитать env для standalone demo helper-а."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value and default:
        return default
    return value


def _build_demo_settings() -> Settings:
    """Собрать partial Settings для standalone secman demo."""
    return Settings.model_construct(
        secman_ssl_verify=_read_demo_env("ALLURE_SECMAN_SSL_VERIFY", "true"),
        secman_addr=_read_demo_env("ALLURE_SECMAN_ADDR"),
        secman_namespace=_read_demo_env("ALLURE_SECMAN_NAMESPACE"),
        secman_k8s_role=_read_demo_env("ALLURE_SECMAN_K8S_ROLE"),
        secman_k8s_jwt_path=_read_demo_env(
            "ALLURE_SECMAN_K8S_JWT_PATH",
            DEFAULT_K8S_JWT_PATH,
        ),
        secman_kv_version=_read_demo_env("ALLURE_SECMAN_KV_VERSION", DEFAULT_KV_VERSION),
        secman_mount_point=_read_demo_env("ALLURE_SECMAN_MOUNT_POINT"),
        secman_secret_path=_read_demo_env("ALLURE_SECMAN_SECRET_PATH"),
    )


def main() -> int:
    """Demo entrypoint для локальной проверки helper-а."""
    try:
        secrets = SecmanClient(_build_demo_settings()).fetch_allure_secrets()
    except ConfigurationError as exc:
        print(f"secman helper error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("secman helper interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"secman helper failed: {exc}", file=sys.stderr)
        return 1

    print("Fetched secrets from secman:")
    for key in ALLURE_SECRET_KEYS:
        print(f"{key}={SecmanClient._mask_secret(secrets[key])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
