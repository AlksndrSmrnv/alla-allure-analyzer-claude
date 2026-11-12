"""Read Qwen's sensitive extension setting through a private subprocess pipe.

Qwen owns encryption and keychain access. The adapter is deliberately versioned:
its credential reader is an internal Qwen API, not a public CLI command.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from alla_core.exceptions import ConfigurationError

EXTENSION_NAME = "alla-launch-credentials"
SETTING_COMMAND = f"qwen extensions settings set {EXTENSION_NAME} ALLURE_TOKEN --scope workspace"
BRIDGE = Path(__file__).resolve().parents[1] / "qwen_credentials.mjs"
PROVENANCE_KEY = "QWEN_CODE_PRIVATE_RELAUNCH_ENV_PROVENANCE"
PLAINTEXT_NOTE = (
    "ALLURE_TOKEN в .env запрещён. Сохраните токен через "
    "qwen extensions settings set alla-launch-credentials ALLURE_TOKEN "
    "--scope workspace и удалите его из .env; для CI используйте окружение"
)
# No model credentials, Node injection flags or debug logging reach the bridge.
BRIDGE_ENV_KEYS = frozenset({
    "HOME", "PATH", "USER", "USERNAME", "SYSTEMROOT", "SystemRoot", "WINDIR",
    "APPDATA", "LOCALAPPDATA", "QWEN_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "GNOME_KEYRING_CONTROL",
    "QWEN_CODE_FORCE_FILE_STORAGE",
})


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
                    "Перенесите токен в хранилище Qwen и удалите открытую копию; "
                    "для CI экспортируйте переменную до запуска Qwen"
                )
    except (ValueError, KeyError, TypeError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось проверить источник окружения Qwen") from None


def qwen_package(environ: Mapping[str, str]) -> Path:
    executable = shutil.which("qwen", path=environ.get("PATH", ""))
    if executable is not None:
        binary = Path(executable).resolve()
        # npm POSIX symlink and Windows npm command shim layouts.
        candidates = [binary.parent, binary.parent / "node_modules/@qwen-code/qwen-code"]
        for candidate in candidates:
            try:
                metadata = json.loads((candidate / "package.json").read_text(encoding="utf-8"))
                if isinstance(metadata, dict) and metadata.get("name") == "@qwen-code/qwen-code":
                    return candidate
            except (OSError, ValueError):
                continue
    raise ConfigurationError(
        "ALLURE_TOKEN: для хранилища нужен Qwen Code 0.27.0 из npm и Node.js в PATH; "
        "для CI передайте токен переменной окружения"
    )


def credential_runtime(package: Path, node: str) -> str:
    """Use Qwen's bundled Bun on macOS, whose native reader works in Seatbelt.

    Node's os.userInfo() cannot query the account database inside Qwen's macOS
    sandbox. Qwen's Bun branch resolves the same OS identity through /usr/bin/id.
    No encryption or key derivation is implemented by this adapter.
    """
    if sys.platform == "darwin":
        arch = {"arm64": "aarch64", "x86_64": "x64"}.get(platform.machine())
        if arch:
            bun = package / "node_modules" / "@oven" / f"bun-darwin-{arch}" / "bin" / "bun"
            if bun.is_file() and os.access(bun, os.X_OK):
                return str(bun)
    return node


def load_qwen_token(project_root: Path | None, environ: Mapping[str, str]) -> str:
    package = qwen_package(environ)
    node = shutil.which("node", path=environ.get("PATH", ""))
    if node is None:
        raise ConfigurationError("ALLURE_TOKEN: для чтения хранилища Qwen нужен Node.js в PATH")
    root = (project_root or Path.cwd()).resolve()
    env = {key: value for key, value in environ.items() if key in BRIDGE_ENV_KEYS}
    runtime = credential_runtime(package, node)
    flags = [f"--config={os.devnull}", "--no-env-file", "--no-install", "--no-macros"] if runtime != node else []
    try:
        result = subprocess.run(
            [runtime, *flags, str(BRIDGE), str(package), str(root)],
            # Never let a workspace configure the credential runtime at startup.
            # The helper changes cwd to root before reading workspace secrets.
            cwd=package, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ConfigurationError("ALLURE_TOKEN: не удалось прочитать хранилище Qwen") from None
    if result.returncode:
        messages = {
            "QWEN_UNSUPPORTED": "адаптер хранилища поддерживает Qwen Code 0.27.0 из npm",
            "QWEN_NOT_INSTALLED": f"установите расширение {EXTENSION_NAME} из папки credentials скилла",
            "QWEN_PLAINTEXT": "удалите ALLURE_TOKEN из .env проекта и расширения Qwen после переноса в хранилище",
            "QWEN_SECRET_MISSING": f"сохраните токен через {SETTING_COMMAND}",
            "QWEN_IDENTITY_UNAVAILABLE": "sandbox не разрешает Node.js узнать имя пользователя; "
                "для macOS нужен Bun из установки Qwen или токен окружения CI",
        }
        message = messages.get(result.stderr.strip(), "не удалось прочитать хранилище Qwen")
        raise ConfigurationError(f"ALLURE_TOKEN: {message}")
    try:
        payload = json.loads(result.stdout)
        if (not isinstance(payload, dict)
                or payload.get("source") not in {"qwen:user", "qwen:workspace"}
                or not isinstance(payload.get("token"), str)
                or not payload["token"].strip()):
            raise ValueError
        return str(payload["token"]).strip()
    except (ValueError, TypeError):
        raise ConfigurationError("ALLURE_TOKEN: некорректный ответ адаптера хранилища Qwen") from None
