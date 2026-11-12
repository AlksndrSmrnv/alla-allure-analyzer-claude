#!/usr/bin/env python3
"""Точка входа скилла alla-launch.

    python3 .qwen/skills/alla-launch/scripts/alla_skill.py setup
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py update --source PATH
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py prepare <launch_id>

Python 3.8+ bootstrap. setup только проверяет готовое окружение и печатает
инструкцию инженеру. update готовит новый локальный релиз с diff, сохраняя активный скилл.

"""

# Аннотации — строками (PEP 563): Python 3.8 их не вычисляет, а mypy проверяет.
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Команды скилла не создают __pycache__ внутри установленной папки .qwen/.
sys.dont_write_bytecode = True

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent


def project_root(skill_dir: Path = SKILL_DIR) -> Path:
    """``<project>/.qwen/skills/<name>`` → ``<project>``, иначе текущая папка.

    Та же логика, что ``workspace.detect_project_root``: здесь её не импортировать —
    ``alla_skill_lib`` требует Python 3.11+.
    """
    if skill_dir.parent.name == "skills" and skill_dir.parent.parent.name == ".qwen":
        candidate = skill_dir.parent.parent.parent.resolve()
        if candidate != Path.home().resolve():
            return candidate
    return Path.cwd().resolve()


PROJECT_DIR = project_root()
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
MIN_VERSION = (3, 11)
FILES_NOTE = (
    "Файлы команд /alla:* и субагентов размещает инженер вручную. "
    "Показать готовые файлы, пути и diff без записи: {}"
)
CHILD_MARKER = "ALLA_SKILL_IN_VENV"


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def running_in_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def self_command(*args: str) -> str:
    parts = ["python" if os.name == "nt" else "python3", str(Path(__file__).resolve())]
    parts.extend(args)
    if os.name == "nt":
        return " ".join('"{}"'.format(p) if " " in p else p for p in parts)
    return shlex.join(parts)


def python_version(executable: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [executable, "-I", "-B", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def venv_needs_ignore(root: Path) -> bool:
    """Git видит ``.venv`` проекта как неотслеживаемую папку (``.gitignore`` скилл не правит).

    Существующий ``.venv`` git проверяет по его типу (у симлинка путь со слэшем — ошибка 128);
    ещё не созданный — как папку. Не git-репозиторий или git не установлен — ``False``.
    """
    path = ".venv" if os.path.lexists(str(root / ".venv")) else ".venv/"
    try:
        code = subprocess.call(
            ["git", "-C", str(root), "check-ignore", "-q", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return code == 1  # 0 — уже игнорируется, 128 — не репозиторий


def locked_requirements() -> list[tuple[str, str, str]]:
    """Accept only exact hashed distributions and the lock's Python version markers.

    A restricted grammar prevents requirements options, URLs and nested files from
    changing which installed dependencies are checked.
    """
    text = REQUIREMENTS.read_text(encoding="utf-8")
    logical = re.sub(r"\\\n\s*", " ", text)
    result = []
    for line in logical.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9_.+!-]+)"
            r"(?:\s*;\s*(python_full_version\s*(?:<|>=|==|<=|>|!=)\s*['\"][0-9.]+['\"]))?"
            r"((?:\s+--hash=sha256:[a-fA-F0-9]{64})+)", line,
        )
        if match is None:
            raise ValueError("requirements.txt: разрешены только точные версии с SHA256 и Python-маркеры")
        result.append((match.group(1), match.group(2), match.group(3) or ""))
    if not result:
        raise ValueError("requirements.txt пуст")
    return result


def validate_dependencies(python: Path, entries: list[tuple[str, str, str]]) -> bool:
    import json

    # No pip and no third-party modules are needed to inspect installed versions.
    code = r"""
import importlib.metadata as m, json, re, sys
entries = json.loads(sys.argv[1])
def applies(marker):
    if not marker:
        return True
    op, raw = re.fullmatch(r"python_full_version\s*(<|>=|==|<=|>|!=)\s*['\"]([0-9.]+)['\"]", marker).groups()
    wanted = tuple(int(v) for v in raw.split('.'))
    current = tuple(sys.version_info[:3])
    wanted = wanted + (0,) * (3 - len(wanted))
    return {'<': current < wanted, '>=': current >= wanted, '==': current == wanted,
            '<=': current <= wanted, '>': current > wanted, '!=': current != wanted}[op]
for name, version, marker in entries:
    if applies(marker):
        try:
            actual = m.version(name)
        except m.PackageNotFoundError:
            sys.exit(1)
        if actual != version:
            sys.exit(1)
"""
    try:
        return subprocess.call(
            [str(python), "-I", "-B", "-c", code, json.dumps(entries)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def environment_status() -> tuple[str, str]:
    """Проверка окружения без маркера, установки пакетов и записи файлов."""
    if not (VENV_DIR.exists() or VENV_DIR.is_symlink()):
        return "setup_required", "Окружения .venv нет: {}".format(VENV_DIR)
    version = python_version(str(venv_python()))
    if version is None:
        return "error", "В проекте есть {}, но Python в нём не запускается.".format(VENV_DIR)
    if version < MIN_VERSION:
        return "error", "{} проекта создан на Python {}.{}, а скиллу нужен {}.{}+.".format(
            VENV_DIR, version[0], version[1], MIN_VERSION[0], MIN_VERSION[1])
    try:
        entries = locked_requirements()
    except (ValueError, OSError) as error:
        return "error", str(error)
    if not validate_dependencies(venv_python(), entries):
        return "setup_required", (
            "Версии зависимостей в {} не соответствуют requirements.txt."
        ).format(VENV_DIR)
    return "ready", "Версии Python и зависимостей проверены: {}".format(VENV_DIR)


def engineer_instructions() -> None:
    """Готовые команды для инженера — только печать, без запуска и запроса согласия."""
    def command(*parts: str) -> str:
        return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)

    print("Дальнейшие действия выполняет инженер вручную; скилл их не запускает:")
    print("1. Если .venv отсутствует, создай её на Python {}.{}+:".format(*MIN_VERSION))
    print(command("python3.11", "-m", "venv", str(VENV_DIR)))
    print("   Существующее окружение проверь и при необходимости исправь вручную.")
    print("2. Установи зависимости из lock-файла разрешённым в вашей среде способом:")
    print(command(str(venv_python()), "-m", "pip", "install", "--require-hashes", "-r", str(REQUIREMENTS)))
    print("3. Если файла настроек нет, скопируй {} в {} и заполни адрес; "
          "токен в .env и в чат не пиши.".format(SKILL_DIR / ".env.example", SKILL_DIR / ".env"))
    if os.name != "nt":
        print(command("chmod", "600", str(SKILL_DIR / ".env")))
    print("   Разрешённый адрес TestOps задаётся инженером в [testops].allowed_origins файла {}.".format(
        PROJECT_DIR / "alla-extensions" / "config.toml"))
    print("   Для хранилища нужны npm Qwen Code 0.27.0 и Node.js в PATH. Из корня проекта:")
    print(command("qwen", "extensions", "install", str(SKILL_DIR / "credentials")))
    print(command("qwen", "extensions", "settings", "set", "alla-launch-credentials",
                  "ALLURE_TOKEN", "--scope", "workspace"))
    print("   Введи токен в запросе Qwen; после переноса удали ALLURE_TOKEN из прежнего .env. "
          "Для CI передай ALLURE_TOKEN переменной окружения.")
    print("4. Повтори проверку окружения и проверь настройки и доступ к TestOps:")
    print(self_command("setup"))
    print(self_command("check"))
    print("5. " + FILES_NOTE.format(self_command("install-files")))
    print("   Размести указанные файлы вручную, затем начни новый сеанс Qwen Code.")


def setup(argv: Sequence[str]) -> int:
    if argv:
        return update_error("setup не принимает аргументы: только проверка окружения и инструкция инженеру.", 2)
    status, message = environment_status()
    print("STATUS: " + status)
    print(message)
    engineer_instructions()
    if venv_needs_ignore(PROJECT_DIR):
        print("git не игнорирует .venv: добавь /.venv в .gitignore проекта сам.")
    return 2 if status == "error" else 0


def update_error(message: str, code: int = 1) -> int:
    print("STATUS: error")
    print(message)
    return code


def update(argv: Sequence[str]) -> int:
    from alla_release import stage_release

    try:
        release, diff = stage_release(argv, PROJECT_DIR, SKILL_DIR)
    except (ValueError, OSError) as error:
        return update_error(str(error), 2)
    print("STATUS: update_ready")
    print("Релиз подготовлен: {}".format(release))
    exclusions = (release.parent / "excluded.txt").read_text(encoding="utf-8").strip()
    if exclusions:
        print("Исключены из релиза (только имена):\n{}".format(exclusions))
    print("Diff: {}".format(diff))
    print("{}".format(diff.read_text(encoding="utf-8")))
    print("Активируй релиз вручную после просмотра diff. Текущий скилл и его .env сохранены; каталоги .qwen не менялись.")
    return 0


def configure_stdio() -> None:
    """UTF-8 для вывода: консоль Windows по умолчанию не UTF-8, а сообщения русские."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str]) -> int:
    configure_stdio()
    if argv[:1] == ["setup"]:
        return setup(argv[1:])
    if argv[:1] == ["update"]:
        return update(argv[1:])

    subargs = list(argv[1:])
    while subargs and subargs[0].split("=", 1)[0] in ("--project-root", "--reports-dir", "--job"):
        option = subargs.pop(0)
        if "=" not in option and subargs:
            subargs.pop(0)
    print_only = argv[:1] == ["install-files"] or (
        argv[:1] == ["extensions"] and bool(subargs) and subargs[0] in ("enable", "disable"))
    removed_before_command = (argv[:1] == ["extensions"] and bool(subargs)
                              and subargs[0].split("=", 1)[0] in ("--yes", "--diff"))
    if removed_before_command or (print_only and any(
            arg in ("--yes", "--diff") or arg.startswith(("--yes=", "--diff=")) for arg in argv)):
        return update_error("--yes и --diff удалены: команда только печатает инструкцию инженеру.", 2)
    status, message = environment_status()
    if status != "ready":
        print("STATUS: " + status)
        print(message)
        print("Проверь окружение и прочитай инструкцию инженеру:")
        print(self_command("setup"))
        print("Не устанавливай пакеты и не меняй настройки автоматически. "
              "После ручной подготовки повтори команду:")
        print(self_command(*argv))
        return 2 if status == "error" else 0

    if running_in_venv() or os.environ.get(CHILD_MARKER):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from alla_skill_lib.cli import main as cli_main

        return cli_main(list(argv))

    env = dict(os.environ, **{CHILD_MARKER: "1"})
    command = [str(venv_python()), "-B", str(Path(__file__).resolve())] + list(argv)
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
