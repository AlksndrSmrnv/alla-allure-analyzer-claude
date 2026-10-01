#!/usr/bin/env python3
"""Точка входа скилла alla-launch.

    python3 .qwen/skills/alla-launch/scripts/alla_skill.py setup [--python PATH] [-- pip-аргументы]
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py prepare <launch_id>
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py next [run_dir]

Файл совместим с Python 3.8+: основная логика выполняется в ``.venv``
скилла (Python 3.11+), куда этот скрипт перезапускает сам себя. ``setup``
создаёт ``.venv`` и ставит зависимости из ``requirements.txt``.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
VENV_DIR = SKILL_DIR / ".venv"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
# Пишется только после успешного pip install: хэш requirements.txt, с которым
# ставились зависимости. Нет маркера или хэш другой — окружение не готово.
SETUP_MARKER = VENV_DIR / ".alla-setup-complete"
MIN_VERSION = (3, 11)
PYTHON_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3", "python")
CHILD_MARKER = "ALLA_SKILL_IN_VENV"


def venv_python(venv_dir=VENV_DIR):
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_digest():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def setup_complete():
    """Окружение создано и зависимости из текущего requirements.txt установлены."""
    try:
        marker = SETUP_MARKER.read_text(encoding="utf-8").strip()
        return venv_python().exists() and marker == requirements_digest()
    except OSError:
        return False


def running_in_venv():
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def self_command(*args):
    parts = ["python" if os.name == "nt" else "python3", str(Path(__file__).resolve())]
    parts.extend(args)
    return " ".join('"{}"'.format(p) if " " in p else p for p in parts)


def python_version(executable):
    try:
        result = subprocess.run(
            [executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        major, minor = result.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def find_base_python(explicit=None):
    if explicit:
        candidates = [explicit]
    else:
        candidates = [sys.executable] + [shutil.which(name) for name in PYTHON_CANDIDATES]
    for candidate in candidates:
        if candidate and (python_version(candidate) or (0, 0)) >= MIN_VERSION:
            return candidate
    return None


def split_setup_args(argv):
    """``[--python PATH] [-- pip-аргументы]`` → (PATH или None, аргументы pip)."""
    argv = list(argv)
    pip_args = []
    if "--" in argv:
        cut = argv.index("--")
        argv, pip_args = argv[:cut], argv[cut + 1:]
    explicit = None
    if argv[:1] == ["--python"] and len(argv) > 1:
        explicit = argv[1]
    return explicit, pip_args


def create_env_file():
    """``.env`` из образца, чтобы пользователю оставалось вписать endpoint и токен."""
    env_file = SKILL_DIR / ".env"
    example = SKILL_DIR / ".env.example"
    if env_file.exists() or not example.exists():
        return None
    shutil.copyfile(str(example), str(env_file))
    if os.name != "nt":
        os.chmod(str(env_file), 0o600)
    return env_file


def setup(argv):
    explicit, pip_args = split_setup_args(argv)
    base = find_base_python(explicit)
    if base is None:
        print("STATUS: error")
        print(
            "Не найден Python {}.{}+. Установи его и повтори, например: {}".format(
                MIN_VERSION[0], MIN_VERSION[1], self_command("setup", "--python", "/path/to/python3.11")
            )
        )
        return 2

    target = venv_python()
    if not target.exists():
        print("Создаю окружение {} (Python: {})".format(VENV_DIR, base), flush=True)
        if subprocess.call([base, "-m", "venv", str(VENV_DIR)]) != 0:
            print("STATUS: error")
            print("Не удалось создать venv. На Debian/Ubuntu нужен пакет python3-venv.")
            return 1

    try:
        SETUP_MARKER.unlink()
    except FileNotFoundError:
        pass
    print(
        "Ставлю зависимости из {} (numpy, scipy, scikit-learn — это 1–3 минуты, "
        "не прерывай).".format(REQUIREMENTS),
        flush=True,
    )
    code = subprocess.call([
        str(target), "-m", "pip", "install", "--disable-pip-version-check",
        "-r", str(REQUIREMENTS),
    ] + pip_args)
    if code != 0:
        print("STATUS: error")
        print(
            "pip install завершился с кодом {}. Проверь доступ к индексу пакетов; для зеркала "
            "или прокси добавь pip-аргументы: {} -- --index-url <URL> --proxy <URL>".format(
                code, self_command("setup")
            )
        )
        return 1
    SETUP_MARKER.write_text(requirements_digest() + "\n", encoding="utf-8")
    print("STATUS: ready")
    print("Окружение скилла готово: {}".format(VENV_DIR))
    created = create_env_file()
    if created is not None:
        print(
            "Создан {}: впиши ALLURE_ENDPOINT и ALLURE_TOKEN (токен в чат не пиши).".format(created)
        )
    return 0


def configure_stdio():
    """UTF-8 для вывода: консоль Windows по умолчанию не UTF-8, а сообщения русские."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv):
    configure_stdio()
    if argv[:1] == ["setup"]:
        return setup(argv[1:])

    if not setup_complete():
        if venv_python().exists():
            state = "не доустановлено или устарело (изменился requirements.txt)"
        else:
            state = "не установлено"
        print("STATUS: setup_required")
        print("Окружение скилла {}. Выполни один раз (займёт пару минут):".format(state))
        print(self_command("setup"))
        print("Затем повтори команду:")
        print(self_command(*argv))
        return 0  # статус с инструкцией, а не авария: агент должен выполнить setup

    if running_in_venv() or os.environ.get(CHILD_MARKER):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from alla_skill_lib.cli import main as cli_main

        return cli_main(argv)

    env = dict(os.environ, **{CHILD_MARKER: "1"})
    command = [str(venv_python()), str(Path(__file__).resolve())] + list(argv)
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
