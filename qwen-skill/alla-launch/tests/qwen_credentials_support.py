"""Native Qwen credential fixtures; use only synthetic secrets in an isolated HOME."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from alla_core import credentials
from alla_core.exceptions import ConfigurationError

SYNTHETIC_TOKEN = "alla-synthetic-credential-not-a-real-token"
SKILL = Path(__file__).resolve().parents[1]


class QwenCredentialUnavailable(RuntimeError):
    """The required qwen or node executable is unavailable."""


def isolated_qwen_env(root: Path) -> dict[str, str]:
    """Never discover personal settings or use the user's OS keychain."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "QWEN_HOME": str(home / ".qwen"),
            "PATH": os.environ.get("PATH", ""), "USER": os.environ.get("USER", "unknown"),
            "QWEN_CODE_FORCE_FILE_STORAGE": "true", "TERM": "xterm"}


def native_qwen_package() -> Path:
    if shutil.which("qwen") is None or shutil.which("node") is None:
        raise QwenCredentialUnavailable("Нужны qwen и node в PATH")
    if os.name == "nt":
        raise RuntimeError("Native credential fixture requires a POSIX PTY")
    try:
        package = credentials.qwen_package(os.environ)
    except ConfigurationError:
        raise RuntimeError("Native credential fixture cannot locate the installed Qwen module") from None
    return package


def qwen_prompt(command: list[str], env: dict[str, str], cwd: Path, token: str) -> None:
    """Enter a synthetic secret at the real CLI password prompt; output stays private."""
    import pty
    import select
    import time

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    output = b""
    sent = False
    deadline = time.monotonic() + 40
    try:
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
                if not sent and b"TestOps API token" in output:
                    os.write(master, (token + "\r").encode())
                    sent = True
        if process.poll() is None:
            process.kill()
        code = process.wait(timeout=5)
        if not sent or code != 0:
            # Do not copy terminal output into exceptions: it can contain input.
            raise RuntimeError(f"Native Qwen credential prompt failed (exit {code}, entered={sent})")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def install_synthetic_credentials(root: Path, project: Path,
                                  token: str = SYNTHETIC_TOKEN) -> dict[str, str]:
    native_qwen_package()
    env = isolated_qwen_env(root)
    qwen = shutil.which("qwen")
    if qwen is None:
        raise QwenCredentialUnavailable("qwen не найден в PATH")
    qwen_prompt([qwen, "extensions", "install", str(SKILL / "credentials"), "--consent"],
                env, project, token)
    return env
