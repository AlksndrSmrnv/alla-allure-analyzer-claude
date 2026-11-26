"""Extension staging and isolated environment helpers; use only synthetic secrets."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SYNTHETIC_TOKEN = "alla-synthetic-credential-not-a-real-token"
SKILL = Path(__file__).resolve().parents[1]
EXTENSION_RUNTIME_ITEMS = (
    "qwen-extension.json",
    "QWENCODE.md",
    "skills",
    "commands",
    "agents",
    "scripts",
    "extensions",
    "requirements.txt",
    "requirements.in",
    ".env.example",
)


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
    """The required qwen executable or OS keyring is unavailable."""


def isolated_qwen_env(root: Path) -> dict[str, str]:
    """Never discover personal Qwen settings."""
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
