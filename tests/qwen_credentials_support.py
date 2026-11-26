"""Isolated keyring and extension staging fixtures; use only synthetic secrets."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SYNTHETIC_TOKEN = "alla-synthetic-credential-not-a-real-token"
SKILL = Path(__file__).resolve().parents[1]
EXTENSION_RUNTIME_ITEMS = (
    "qwen-extension.json",
    "skills",
    "commands",
    "agents",
    "scripts",
    "extensions",
    "requirements.txt",
    "requirements.in",
    ".env.example",
)

_ISOLATED_BACKEND_CODE = '''"""Test-only isolated file-backed keyring backend (never used in production)."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

STORE_PATH = Path(__file__).resolve().parent.parent / "store.json"


def _obfuscate(value: str) -> str:
    # Keep plaintext synthetic token bytes out of the test store file.
    return base64.b64encode(bytes(b ^ 0x5A for b in value.encode("utf-8"))).decode("ascii")


def _deobfuscate(encoded: str) -> str:
    raw = base64.b64decode(encoded.encode("ascii"))
    return bytes(b ^ 0x5A for b in raw).decode("utf-8")


class IsolatedFileKeyring(KeyringBackend):
    priority = 10

    def _read(self) -> dict[str, str]:
        if not STORE_PATH.is_file():
            return {}
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.chmod(STORE_PATH, 0o600)

    def get_password(self, service: str, username: str) -> str | None:
        encoded = self._read().get(f"{service}\\0{username}")
        return _deobfuscate(encoded) if isinstance(encoded, str) else None

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._read()
        data[f"{service}\\0{username}"] = _obfuscate(password)
        self._write(data)

    def delete_password(self, service: str, username: str) -> None:
        data = self._read()
        key = f"{service}\\0{username}"
        if key not in data:
            raise PasswordDeleteError("not found")
        del data[key]
        self._write(data)
'''


def stage_extension(dest: Path) -> Path:
    """Copy only runtime extension files so ``qwen extensions install`` skips tests/venv/git."""
    dest.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.egg-info")
    for name in EXTENSION_RUNTIME_ITEMS:
        src = SKILL / name
        if not src.exists():
            continue
        dst = dest / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return dest


class QwenCredentialUnavailable(RuntimeError):
    """The required qwen executable is unavailable."""


def isolated_qwen_env(root: Path) -> dict[str, str]:
    """Never discover personal settings or use the user's OS keychain."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    config_home = home / ".config"
    config_home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "QWEN_HOME": str(home / ".qwen"),
        "XDG_CONFIG_HOME": str(config_home),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", "unknown"),
        "TERM": "xterm",
    }


def install_isolated_keyring_backend(home: Path) -> Path:
    """Configure an isolated test keyring backend inside ``home`` (never touches OS keychain)."""
    config_dir = home / ".config" / "python_keyring"
    backend_dir = config_dir / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "isolated_keyring_backend.py").write_text(_ISOLATED_BACKEND_CODE, encoding="utf-8")
    (config_dir / "keyringrc.cfg").write_text(
        "[backend]\n"
        "default-keyring=isolated_keyring_backend.IsolatedFileKeyring\n"
        f"keyring-path={backend_dir}\n",
        encoding="utf-8",
    )
    return config_dir / "store.json"
