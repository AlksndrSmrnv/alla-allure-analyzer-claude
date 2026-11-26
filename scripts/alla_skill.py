#!/usr/bin/env python3
"""Точка входа расширения Qwen Code ``alla``.

    python3 <extension>/scripts/alla_skill.py setup
    python3 <extension>/scripts/alla_skill.py check
    python3 <extension>/scripts/alla_skill.py prepare <launch_id>

Python 3.8+ bootstrap. setup только проверяет готовое окружение и печатает
инструкцию инженеру.
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

# Команды расширения не создают __pycache__ внутри установленной папки Qwen.
sys.dont_write_bytecode = True

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent


def project_root(_skill_dir: Path = SKILL_DIR) -> Path:
    """Корень проекта автотестов — текущая рабочая папка, из которой запущен Qwen Code."""
    return Path.cwd().resolve()


PROJECT_DIR = project_root()
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
MIN_VERSION = (3, 11)
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


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_applies(marker: str, version_info: tuple[int, int, int]) -> bool:
    if not marker:
        return True
    match = re.fullmatch(
        r"python_full_version\s*(<|>=|==|<=|>|!=)\s*['\"]([0-9.]+)['\"]",
        marker,
    )
    if match is None:
        return False
    op, raw = match.groups()
    wanted = tuple(int(part) for part in raw.split("."))
    wanted = wanted + (0,) * (3 - len(wanted))
    if op == "<":
        return version_info < wanted
    if op == ">=":
        return version_info >= wanted
    if op == "==":
        return version_info == wanted
    if op == "<=":
        return version_info <= wanted
    if op == ">":
        return version_info > wanted
    return version_info != wanted


def _validate_dependencies_in_process(entries: list[tuple[str, str, str]]) -> bool:
    """Validate locked distributions inside current venv without reading PYTHONPATH or cwd."""
    import importlib.metadata as metadata
    import site
    import sysconfig

    sys_paths = sysconfig.get_paths()
    get_site = getattr(site, "getsitepackages", None)
    site_paths = list(get_site()) if get_site is not None else []
    raw_paths = site_paths + [
        sys_paths["purelib"],
        sys_paths["platlib"],
    ]
    paths: list[str] = []
    for candidate in raw_paths:
        if candidate and candidate not in paths:
            paths.append(candidate)

    installed: dict[str, str] = {}
    for dist in metadata.distributions(path=paths):
        raw_name = getattr(dist, "name", None)
        if not raw_name and dist.metadata is not None:
            raw_name = dist.metadata["Name"]
        if raw_name:
            installed.setdefault(_normalize_dist_name(str(raw_name)), dist.version)

    current = (sys.version_info[0], sys.version_info[1], sys.version_info[2])
    for name, version, marker in entries:
        if _marker_applies(marker, current):
            if installed.get(_normalize_dist_name(name)) != version:
                return False
    return True


# Keep `print` in the inline probe so minimal mock wrappers in tests (`*print*`) still respond.
_PROBE_CODE = r"""
import json, re, sys
if len(sys.argv) > 2 and sys.argv[2]:
    min_ver = tuple(int(v) for v in sys.argv[2].split('.'))
    if sys.version_info[:2] < min_ver:
        print("old %d.%d" % sys.version_info[:2])
        sys.exit(0)
import importlib.metadata as m
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
            print("deps")
            sys.exit(0)
        if actual != version:
            print("deps")
            sys.exit(0)
print("ok")
"""


def _probe_environment(
    python: Path,
    entries: list[tuple[str, str, str]],
    min_version: tuple[int, int] | None = MIN_VERSION,
) -> tuple[str, tuple[int, int] | None]:
    """Check interpreter health, Python version and locked packages in one isolated subprocess."""
    import json

    min_arg = "{}.{}".format(min_version[0], min_version[1]) if min_version is not None else ""
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", _PROBE_CODE, json.dumps(entries), min_arg],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "broken", None
    if result.returncode != 0:
        return "broken", None
    out = result.stdout.strip()
    if out == "ok":
        return "ok", None
    if out == "deps":
        return "deps", None
    if out.startswith("old "):
        try:
            major, minor = out[4:].strip().split(".")
            return "old", (int(major), int(minor))
        except ValueError:
            return "broken", None
    # Compatibility with test wrappers (e.g. `echo 3.9` or `echo 3.11` for `*print*`).
    if re.fullmatch(r"\d+\.\d+", out):
        major, minor = out.split(".")
        parsed = (int(major), int(minor))
        if min_version is not None and parsed < min_version:
            return "old", parsed
        return "deps", None
    return "broken", None


def validate_dependencies(python: Path, entries: list[tuple[str, str, str]]) -> bool:
    state, _ = _probe_environment(python, entries, min_version=None)
    return state == "ok"


def environment_status() -> tuple[str, str]:
    """Проверка окружения без маркера, установки пакетов и записи файлов."""
    if not (VENV_DIR.exists() or VENV_DIR.is_symlink()):
        return "setup_required", "Окружения .venv нет: {}".format(VENV_DIR)
    if running_in_venv():
        current = (sys.version_info[0], sys.version_info[1])
        if current < MIN_VERSION:
            return "error", "{} проекта создан на Python {}.{}, а скиллу нужен {}.{}+.".format(
                VENV_DIR, current[0], current[1], MIN_VERSION[0], MIN_VERSION[1])
        try:
            entries = locked_requirements()
        except (ValueError, OSError) as error:
            return "error", str(error)
        if not _validate_dependencies_in_process(entries):
            return "setup_required", (
                "Версии зависимостей в {} не соответствуют requirements.txt."
            ).format(VENV_DIR)
        return "ready", "Версии Python и зависимостей проверены: {}".format(VENV_DIR)

    try:
        entries = locked_requirements()
    except (ValueError, OSError) as error:
        return "error", str(error)
    state, version = _probe_environment(venv_python(), entries)
    if state == "broken":
        return "error", "В проекте есть {}, но Python в нём не запускается.".format(VENV_DIR)
    if state == "old" and version is not None:
        return "error", "{} проекта создан на Python {}.{}, а скиллу нужен {}.{}+.".format(
            VENV_DIR, version[0], version[1], MIN_VERSION[0], MIN_VERSION[1])
    if state != "ok":
        return "setup_required", (
            "Версии зависимостей в {} не соответствуют requirements.txt."
        ).format(VENV_DIR)
    return "ready", "Версии Python и зависимостей проверены: {}".format(VENV_DIR)


def engineer_instructions() -> None:
    """Готовые команды для инженера — только печать, без запуска и запроса согласия."""
    def command(*parts: str) -> str:
        return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)

    project_env = PROJECT_DIR / ".qwen" / "alla.env"
    print("Дальнейшие действия выполняет инженер вручную; расширение их не запускает:")
    print("1. Если .venv отсутствует, создай её на Python {}.{}+:".format(*MIN_VERSION))
    print(command("python3.11", "-m", "venv", str(VENV_DIR)))
    print("   Существующее окружение проверь и при необходимости исправь вручную.")
    print("2. Установи зависимости из lock-файла разрешённым в вашей среде способом:")
    print(command(str(venv_python()), "-m", "pip", "install", "--require-hashes", "-r", str(REQUIREMENTS)))
    print("3. Если файла настроек нет, скопируй {} в {} (или в {}) и заполни адрес; "
          "токен в .env и в чат не пиши.".format(SKILL_DIR / ".env.example", project_env, SKILL_DIR / ".env"))
    if os.name != "nt":
        print(command("mkdir", "-p", str(project_env.parent)))
        print(command("cp", str(SKILL_DIR / ".env.example"), str(project_env)))
        print(command("chmod", "600", str(project_env)))
    print("   Разрешённый адрес TestOps задаётся инженером в [testops].allowed_origins файла {}.".format(
        PROJECT_DIR / "alla-extensions" / "config.toml"))
    print("   Сохрани личный API-токен TestOps в хранилище Qwen Code (из корня проекта):")
    print(command("qwen", "extensions", "settings", "set", "alla",
                  "ALLURE_TOKEN", "--scope", "workspace"))
    print("   Введи токен в запросе Qwen; для общего токена используй --scope user, "
          "для CI передай ALLURE_TOKEN переменной окружения.")
    print("4. Повтори проверку окружения и проверь настройки и доступ к TestOps:")
    print(self_command("setup"))
    print(self_command("check"))
    print("   Команды /alla:* и субагенты загружаются самим расширением alla (перезапусти сеанс Qwen Code при обновлении).")


def setup(argv: Sequence[str]) -> int:
    if argv:
        return _bootstrap_error("setup не принимает аргументы: только проверка окружения и инструкция инженеру.", 2)
    status, message = environment_status()
    print("STATUS: " + status)
    print(message)
    engineer_instructions()
    if venv_needs_ignore(PROJECT_DIR):
        print("git не игнорирует .venv: добавь /.venv в .gitignore проекта сам.")
    return 2 if status == "error" else 0


def _bootstrap_error(message: str, code: int = 1) -> int:
    print("STATUS: error")
    print(message)
    return code


def _explicit_project_root(argv: Sequence[str]) -> Path | None:
    for idx, arg in enumerate(argv):
        if arg == "--project-root" and idx + 1 < len(argv):
            return Path(argv[idx + 1]).expanduser().resolve()
        if arg.startswith("--project-root="):
            return Path(arg.split("=", 1)[1]).expanduser().resolve()
    return None


def configure_stdio() -> None:
    """UTF-8 для вывода: консоль Windows по умолчанию не UTF-8, а сообщения русские."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str]) -> int:
    global PROJECT_DIR, VENV_DIR
    configure_stdio()
    explicit_root = _explicit_project_root(argv)
    if explicit_root is not None:
        PROJECT_DIR = explicit_root
        VENV_DIR = PROJECT_DIR / ".venv"
    if argv[:1] == ["setup"]:
        return setup(argv[1:])

    subargs = list(argv[1:])
    while subargs and subargs[0].split("=", 1)[0] in ("--project-root", "--reports-dir", "--job"):
        option = subargs.pop(0)
        if "=" not in option and subargs:
            subargs.pop(0)
    toggle = argv[:1] == ["extensions"] and bool(subargs) and subargs[0] in ("enable", "disable")
    removed_before_command = (argv[:1] == ["extensions"] and bool(subargs)
                              and subargs[0].split("=", 1)[0] in ("--yes", "--diff"))
    if removed_before_command or (toggle and any(
            arg in ("--yes", "--diff") or arg.startswith(("--yes=", "--diff=")) for arg in argv)):
        return _bootstrap_error("--yes и --diff удалены: команда только печатает инструкцию инженеру.", 2)
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
