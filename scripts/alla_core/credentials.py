"""Read and manage ALLURE_TOKEN via OS keyring without plaintext files or Qwen internals."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alla_core.exceptions import ConfigurationError

KEYRING_SERVICE = "alla-allure-testops"
SCOPES = ("workspace", "user")
_ENTRYPOINT = Path(__file__).resolve().parents[1] / "alla_skill.py"
SETTING_COMMAND = (
    f'python "{_ENTRYPOINT}" token set' if os.name == "nt" else shlex.join(["python3", str(_ENTRYPOINT), "token", "set"])
)
PROVENANCE_KEY = "QWEN_CODE_PRIVATE_RELAUNCH_ENV_PROVENANCE"
PLAINTEXT_NOTE = (
    "ALLURE_TOKEN в .env запрещён. Сохраните токен в системном хранилище ОС командой "
    f"{SETTING_COMMAND} и удалите его из .env; для CI используйте окружение"
)
_NO_KEYRING_MESSAGE = (
    "ALLURE_TOKEN: недоступно системное хранилище ключей ОС "
    "(на macOS — Keychain, на Linux — GNOME Keyring/KWallet через Secret Service, "
    "на Windows — Credential Manager; проверьте `keyring diagnose`). "
    "Для CI передайте токен переменной окружения ALLURE_TOKEN"
)


@dataclass(frozen=True)
class TokenStatus:
    """Sanitized summary of token availability without holding any secret value."""

    backend: str
    backend_available: bool
    has_env: bool
    has_workspace: bool
    has_user: bool
    active_source: str


def reject_plaintext_token(text: str) -> None:
    """Inspect declarations, including duplicates hidden by dotenv's last value."""
    for line in text.lstrip("\ufeff").splitlines():
        match = re.match(r"^\s*(?:export\s+)?ALLURE_TOKEN\s*(?:=|:\s)(.*)$", line)
        if match is None:
            continue
        value = match.group(1).strip()
        # Empty assignments and empty quoted values with optional comments carry no secret.
        if value and not value.startswith("#") and not re.fullmatch(r"(?:\"\"|'')\s*(?:#.*)?", value):
            raise ConfigurationError(PLAINTEXT_NOTE)


def validate_environment_token(environ: Mapping[str, str]) -> None:
    """Qwen dotenv/settings.env imports are plaintext, even after becoming env vars."""
    raw = environ.get(PROVENANCE_KEY)
    if not environ.get("ALLURE_TOKEN", "").strip() or raw is None:
        return
    try:
        provenance = json.loads(raw)
        if not isinstance(provenance, dict):
            raise ValueError
        for source in ("dotEnv", "settingsEnv"):
            keys = provenance[source]
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                raise ValueError
            if any(key.upper() == "ALLURE_TOKEN" for key in keys):
                raise ConfigurationError(
                    "ALLURE_TOKEN получен Qwen из .env или settings.json.env. "
                    f"Перенесите токен в системное хранилище ОС ({SETTING_COMMAND}) "
                    "и удалите открытую копию; для CI экспортируйте переменную до запуска Qwen"
                )
    except (ValueError, KeyError, TypeError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось проверить источник окружения Qwen") from None


def workspace_account(project_root: Path) -> str:
    """Return a stable keyring account key scoped to a project directory."""
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:24]
    return f"workspace:{digest}"


def account_for(scope: str, project_root: Path | None) -> str:
    """Resolve keyring account name for ``workspace`` or ``user`` scope."""
    if scope == "user":
        return "user"
    if scope == "workspace":
        return workspace_account(project_root or Path.cwd())
    raise ValueError(f"Unsupported token scope: {scope}")


def _unwrap_backends(backend: Any, fail_cls: type[Any], null_cls: type[Any], chainer_cls: type[Any]) -> list[Any]:
    if isinstance(backend, chainer_cls):
        return [item for item in backend.backends if not isinstance(item, (fail_cls, null_cls))]
    if isinstance(backend, (fail_cls, null_cls)):
        return []
    return [backend]


def describe_backend(backend: Any) -> str:
    """Return a concise, human-readable backend class path without secrets."""
    import keyring.backends.chainer as chainer_mod
    import keyring.backends.fail as fail_mod
    import keyring.backends.null as null_mod

    inner = _unwrap_backends(backend, fail_mod.Keyring, null_mod.Keyring, chainer_mod.ChainerBackend)
    target = inner[0] if inner else backend
    return f"{type(target).__module__}.{type(target).__qualname__}"


def _active_keyring() -> Any:
    """Lazily import keyring and ensure a real OS credential backend is active."""
    import keyring
    import keyring.backends.chainer as chainer_mod
    import keyring.backends.fail as fail_mod
    import keyring.backends.null as null_mod
    import keyring.errors as kr_errors

    try:
        backend = keyring.get_keyring()
    except (kr_errors.KeyringError, OSError, RuntimeError):
        raise ConfigurationError(_NO_KEYRING_MESSAGE) from None
    inner = _unwrap_backends(backend, fail_mod.Keyring, null_mod.Keyring, chainer_mod.ChainerBackend)
    if not inner:
        raise ConfigurationError(_NO_KEYRING_MESSAGE)
    return backend


def _clean_token(token: str) -> str:
    if "\r" in token or "\n" in token or "\x00" in token:
        raise ConfigurationError("ALLURE_TOKEN: токен не должен содержать переводы строк или NUL")
    cleaned = token.strip()
    if not cleaned:
        raise ConfigurationError("ALLURE_TOKEN: пустой токен не сохранён")
    return cleaned


def _get_password(backend: Any, account: str) -> str:
    import keyring.errors as kr_errors

    try:
        raw = backend.get_password(KEYRING_SERVICE, account)
    except (kr_errors.KeyringError, OSError, RuntimeError, ValueError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось прочитать системное хранилище ключей ОС") from None
    return raw.strip() if isinstance(raw, str) else ""


def load_token(project_root: Path | None) -> str:
    """Load ALLURE_TOKEN from OS keyring (workspace scope takes precedence over user)."""
    backend = _active_keyring()
    accounts = [workspace_account(project_root), "user"] if project_root is not None else ["user"]
    for account in accounts:
        token = _get_password(backend, account)
        if token:
            return token
    raise ConfigurationError(
        "ALLURE_TOKEN: токен не найден в системном хранилище ОС. "
        f"Сохраните его командой {SETTING_COMMAND} (или с --scope user); "
        "для CI передайте переменную окружения ALLURE_TOKEN"
    )


def store_token(token: str, *, scope: str = "workspace", project_root: Path | None = None) -> str:
    """Store ALLURE_TOKEN in OS keyring under the requested scope and return backend name."""
    import keyring.errors as kr_errors

    cleaned = _clean_token(token)
    account = account_for(scope, project_root)
    backend = _active_keyring()
    try:
        backend.set_password(KEYRING_SERVICE, account, cleaned)
    except (kr_errors.KeyringError, OSError, RuntimeError, NotImplementedError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось сохранить токен в системном хранилище ОС") from None
    return describe_backend(backend)


def delete_token(*, scope: str = "workspace", project_root: Path | None = None) -> bool:
    """Delete ALLURE_TOKEN from OS keyring if present; return True when deleted."""
    import keyring.errors as kr_errors

    account = account_for(scope, project_root)
    backend = _active_keyring()
    try:
        existing = backend.get_password(KEYRING_SERVICE, account)
    except (kr_errors.KeyringError, OSError, RuntimeError, ValueError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось прочитать системное хранилище ключей ОС") from None
    if existing is None:
        return False
    try:
        backend.delete_password(KEYRING_SERVICE, account)
    except (kr_errors.KeyringError, OSError, RuntimeError, NotImplementedError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось удалить токен из системного хранилища ОС") from None
    return True


def token_status(project_root: Path | None, environ: Mapping[str, str]) -> TokenStatus:
    """Inspect where ALLURE_TOKEN is configured without returning its value."""
    validate_environment_token(environ)
    has_env = bool(environ.get("ALLURE_TOKEN", "").strip())
    backend_available = False
    backend_name = ""
    has_workspace = False
    has_user = False
    try:
        backend = _active_keyring()
    except ConfigurationError:
        backend = None
    if backend is not None:
        backend_available = True
        backend_name = describe_backend(backend)
        if project_root is not None:
            has_workspace = bool(_get_password(backend, workspace_account(project_root)))
        has_user = bool(_get_password(backend, "user"))
    if has_env:
        active_source = "env"
    elif has_workspace:
        active_source = "keyring:workspace"
    elif has_user:
        active_source = "keyring:user"
    else:
        active_source = ""
    return TokenStatus(
        backend=backend_name,
        backend_available=backend_available,
        has_env=has_env,
        has_workspace=has_workspace,
        has_user=has_user,
        active_source=active_source,
    )
