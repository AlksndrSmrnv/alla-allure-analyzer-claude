#!/usr/bin/env python3
"""Точка входа скилла alla-launch.

    python3 .qwen/skills/alla-launch/scripts/alla_skill.py setup [--wheelhouse PATH] [--python PATH]
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py update --source PATH
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py prepare <launch_id>

Python 3.8+ bootstrap. setup проверяет готовое окружение без установки;
только явный --wheelhouse разрешает локальную установку. update готовит
новый локальный релиз с diff, сохраняя активный скилл.

"""

# Аннотации — строками (PEP 563): Python 3.8 их не вычисляет, а mypy проверяет.
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

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
# Пишется только после успешного pip install: хэш requirements.txt, с которым
# ставились зависимости. Нет маркера или хэш другой — окружение не готово.
SETUP_MARKER = VENV_DIR / ".alla-setup-complete"
MIN_VERSION = (3, 11)
# setup и update в .qwen/ проекта не пишут: команды /alla:* и субагента ставит install-files
# после согласия пользователя.
FILES_NOTE = (
    "Слэш-команды /alla:* и субагент пакетов в .qwen/ проекта setup и update не ставят: это "
    "отдельный шаг с вопросом пользователю (порядок — SKILL.md, «Файлы в .qwen/ проекта»), "
    "сразу эту команду не выполняй. Показ файлов (ничего не записывает): {}"
)
PYTHON_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3", "python")
CHILD_MARKER = "ALLA_SKILL_IN_VENV"


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_digest() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def setup_complete() -> bool:
    """Окружение создано и зависимости из текущего requirements.txt установлены."""
    try:
        marker = SETUP_MARKER.read_text(encoding="utf-8").strip()
        return venv_python().exists() and marker == requirements_digest()
    except OSError:
        return False


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


def find_base_python(explicit: str | None = None) -> str | None:
    if explicit:
        candidates: list[str | None] = [explicit]
    else:
        candidates = [sys.executable, *(shutil.which(name) for name in PYTHON_CANDIDATES)]
    for candidate in candidates:
        if candidate and (python_version(candidate) or (0, 0)) >= MIN_VERSION:
            return candidate
    return None


def split_setup_args(argv: Sequence[str]) -> tuple[str | None, Path | None]:
    """Only explicit local wheelhouse installation is supported."""
    args = list(argv)
    found: dict[str, str] = {}
    while args:
        name = args.pop(0)
        if name not in ("--python", "--wheelhouse") or name in found or not args:
            raise ValueError("setup [--wheelhouse PATH] [--python PATH]; pip-аргументы не принимаются")
        found[name] = args.pop(0)
    if "--python" in found and "--wheelhouse" not in found:
        raise ValueError("--python используется только вместе с --wheelhouse")
    return found.get("--python"), Path(found["--wheelhouse"]) if "--wheelhouse" in found else None


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


def create_env_file() -> Path | None:
    """``.env`` из образца, чтобы пользователю оставалось вписать endpoint и токен."""
    env_file = SKILL_DIR / ".env"
    example = SKILL_DIR / ".env.example"
    if os.path.lexists(str(env_file)) or not example.exists():
        return None
    shutil.copyfile(str(example), str(env_file))
    if os.name != "nt":
        os.chmod(str(env_file), 0o600)
    return env_file


def use_existing_venv(explicit: str | None) -> bool:
    """Существующий ``.venv`` проекта годится: Python в нём запускается и он 3.11+."""
    version = python_version(str(venv_python()))
    wanted = "{}.{}".format(*MIN_VERSION)
    if version is None:
        print("STATUS: error")
        print(
            "В проекте есть {}, но Python в нём не запускается. Пересоздай этот .venv на "
            "Python {}+ (и поставь в него зависимости проекта заново) или удали его, затем "
            "повтори: {}".format(VENV_DIR, wanted, self_command("setup"))
        )
        return False
    if version < MIN_VERSION:
        print("STATUS: error")
        print(
            "{} проекта создан на Python {}.{}, а скиллу нужен {}+. Пересоздай этот .venv на "
            "Python {}+ (и поставь в него зависимости проекта заново), затем повтори: {}".format(
                VENV_DIR, version[0], version[1], wanted, wanted, self_command("setup")
            )
        )
        return False
    if explicit:
        print("{} уже есть — --python не используется.".format(VENV_DIR), flush=True)
    return True


def create_venv(explicit: str | None) -> int:
    """Создать ``.venv`` проекта; 0 — создан, иначе код возврата ``setup``."""
    base = find_base_python(explicit)
    if base is None:
        print("STATUS: error")
        print(
            "Не найден Python {}.{}+. Установи его и повтори, например: {}".format(
                MIN_VERSION[0], MIN_VERSION[1], self_command("setup", "--wheelhouse", "/local/wheels", "--python", "/path/to/python3.11")
            )
        )
        return 2
    print("Создаю окружение {} (Python: {})".format(VENV_DIR, base), flush=True)
    if subprocess.call([base, "-m", "venv", str(VENV_DIR)]) != 0:
        print("STATUS: error")
        print("Не удалось создать venv. На Debian/Ubuntu нужен пакет python3-venv.")
        return 1
    return 0


def ensure_pip(python: Path) -> bool:
    """pip в ``.venv``: окружение могли создать с ``--without-pip``.

    ``ensurepip`` ставит pip из колёс, вшитых в Python, — без сети и зеркала.
    """
    check = [str(python), "-I", "-m", "pip", "--version"]
    if subprocess.call(check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    print("В {} нет pip — ставлю его через ensurepip.".format(VENV_DIR), flush=True)
    if subprocess.call([str(python), "-I", "-m", "ensurepip", "--upgrade"]) == 0:
        return True
    print("STATUS: error")
    print(
        "В {} нет pip, и ensurepip не смог его поставить (на Debian/Ubuntu для ensurepip нужен "
        "пакет python3-venv). Поставь pip в этот .venv сам: {} -m ensurepip --upgrade — или "
        "пересоздай .venv на Python {}.{}+, затем повтори: {}".format(
            VENV_DIR, python, MIN_VERSION[0], MIN_VERSION[1], self_command("setup")
        )
    )
    return False


def locked_requirements() -> list[tuple[str, str, str]]:
    """Accept only exact hashed distributions and the lock's Python version markers.

    A restricted grammar prevents requirements options, URLs and nested files from
    turning the offline installation into another source of dependencies.
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
            [str(python), "-I", "-c", code, json.dumps(entries)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        ) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup(argv: Sequence[str]) -> int:
    try:
        explicit, wheelhouse = split_setup_args(argv)
        entries = locked_requirements()
        if wheelhouse is not None:
            wheelhouse = wheelhouse.resolve(strict=True)
            if not wheelhouse.is_dir():
                raise ValueError("--wheelhouse должен указывать на локальный каталог")
    except (ValueError, OSError) as error:
        return update_error(str(error), 2)
    if VENV_DIR.exists() or VENV_DIR.is_symlink():
        if not use_existing_venv(explicit):
            return 2
    elif wheelhouse is None:
        print("STATUS: setup_required")
        print("Окружения .venv нет. Установи зависимости вручную либо явно укажи setup --wheelhouse /local/path. Автоматической установки нет.")
        return 0
    else:
        code = create_venv(explicit)
        if code:
            return code
    target = venv_python()
    if wheelhouse is not None:
        from alla_wheels import local_wheels

        try:
            with local_wheels(wheelhouse) as wheels:
                SETUP_MARKER.unlink(missing_ok=True)
                if not ensure_pip(target):
                    return 1
                env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
                env["PIP_CONFIG_FILE"] = os.devnull
                code = subprocess.call([
                    str(target), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-index", "--find-links", str(wheels), "--only-binary=:all:", "--no-deps",
                    "--require-hashes", "-r", str(REQUIREMENTS),
                ], env=env)
        except (ValueError, OSError) as error:
            return update_error(str(error), 2)
        if code:
            return update_error("Не удалось установить зависимости из локального wheelhouse; сеть не используется.")
    if not validate_dependencies(target, entries):
        # A stale marker must not claim that a failed validation was successful.
        SETUP_MARKER.unlink(missing_ok=True)
        print("STATUS: setup_required")
        print("Версии зависимостей не соответствуют requirements.txt. Установи их вручную либо явно выполни setup --wheelhouse /local/path.")
        return 0
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(VENV_DIR),
                                     prefix=".alla-setup-", delete=False) as handle:
        handle.write(requirements_digest() + "\n")
        marker_tmp = Path(handle.name)
    try:
        os.replace(str(marker_tmp), str(SETUP_MARKER))
    finally:
        marker_tmp.unlink(missing_ok=True)
    if venv_needs_ignore(PROJECT_DIR):
        print("git не игнорирует .venv: добавь /.venv в .gitignore проекта сам.", flush=True)
    print("STATUS: ready")
    print("Версии зависимостей проверены: {}".format(VENV_DIR))
    created = create_env_file()
    if created is not None:
        print("Создан {}: впиши адрес и токен (токен в чат не пиши).".format(created))
    print(FILES_NOTE.format(self_command("install-files")))
    return 0


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

    if not setup_complete():
        if venv_python().exists():
            state = "не доустановлены или устарели (изменился requirements.txt)"
        else:
            state = "не установлены"
        print("STATUS: setup_required")
        print(
            "Зависимости скилла в .venv проекта ({}) {}. Проверь уже установленное окружение:".format(VENV_DIR, state)
        )
        print(self_command("setup"))
        print("Не устанавливай пакеты автоматически. После ручной установки или явного --wheelhouse повтори команду:")
        print(self_command(*argv))
        return 0  # агент останавливается; установка требует явного локального источника

    if running_in_venv() or os.environ.get(CHILD_MARKER):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from alla_skill_lib.cli import main as cli_main

        return cli_main(list(argv))

    env = dict(os.environ, **{CHILD_MARKER: "1"})
    command = [str(venv_python()), str(Path(__file__).resolve())] + list(argv)
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
