#!/usr/bin/env python3
"""Точка входа скилла alla-launch.

    python3 .qwen/skills/alla-launch/scripts/alla_skill.py setup [--python PATH] [-- pip-аргументы]
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py update [--source URL] [--ref REF]
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py prepare <launch_id>
    python3 .qwen/skills/alla-launch/scripts/alla_skill.py next [run_dir]

Файл совместим с Python 3.8+: основная логика выполняется в ``.venv`` в корне
проекта автотестов (Python 3.11+), куда этот скрипт перезапускает сам себя.
``setup`` создаёт этот ``.venv`` (или берёт существующий) и ставит в него
зависимости из ``requirements.txt``. ``update`` заменяет папку скилла версией из
git-репозитория (адрес — таблица ``[update]`` в ``alla-extensions/config.toml``); как и
``setup``, он работает без ``.venv``.
"""

# Аннотации — строками (PEP 563): Python 3.8 их не вычисляет, а mypy проверяет.
from __future__ import annotations

import filecmp
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
PYTHON_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3", "python")
CHILD_MARKER = "ALLA_SKILL_IN_VENV"
# Коммит, из которого установлен скилл (пишет update); в .gitignore скилла.
VERSION_MARKER = SKILL_DIR / ".alla-version"
# Пользовательское в папке скилла: update переносит это в новую версию как есть.
UPDATE_KEEP = (".env", ".venv")
UPDATE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".env", ".venv")
UPDATE_SOURCE_PATHS = ("qwen-skill/alla-launch", "")
GIT_TIMEOUT = 90  # Qwen Code обрывает команду shell на 120 с


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


def split_setup_args(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """``[--python PATH] [-- pip-аргументы]`` → (PATH или None, аргументы pip)."""
    argv = list(argv)
    pip_args: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, pip_args = argv[:cut], argv[cut + 1:]
    explicit = None
    if argv[:1] == ["--python"] and len(argv) > 1:
        explicit = argv[1]
    return explicit, pip_args


def ensure_gitignored(root: Path) -> Path | None:
    """Дописать ``/.venv`` в ``<root>/.gitignore``, если git его не игнорирует.

    ``/.venv`` без слэша на конце: ``.venv`` может быть и симлинком, а шаблон ``.venv/``
    совпадает только с папкой. Существующий ``.venv`` git проверяет по его типу
    (у симлинка путь со слэшем — ошибка 128); ещё не созданный — как папку.
    Не git-репозиторий или git не установлен — ничего не делать.
    """
    path = ".venv" if os.path.lexists(str(root / ".venv")) else ".venv/"
    try:
        code = subprocess.call(
            ["git", "-C", str(root), "check-ignore", "-q", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if code != 1:  # 0 — уже игнорируется, 128 — не репозиторий
        return None
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    prefix = "\n" if text and not text.endswith("\n") else ""
    with gitignore.open("a", encoding="utf-8") as stream:
        stream.write(prefix + "/.venv\n")
    return gitignore


def create_env_file() -> Path | None:
    """``.env`` из образца, чтобы пользователю оставалось вписать endpoint и токен."""
    env_file = SKILL_DIR / ".env"
    example = SKILL_DIR / ".env.example"
    if env_file.exists() or not example.exists():
        return None
    shutil.copyfile(str(example), str(env_file))
    if os.name != "nt":
        os.chmod(str(env_file), 0o600)
    return env_file


def install_commands(project_dir: Path = PROJECT_DIR) -> list[Path]:
    """Команды ``/alla:*`` в ``<проект>/.qwen/commands/alla/``; возвращает записанные файлы.

    Та же логика, что ``qwen_files.install_commands``: здесь её не импортировать —
    ``alla_skill_lib`` требует Python 3.11+.
    """
    target_dir = project_dir / ".qwen" / "commands" / "alla"
    written: list[Path] = []
    for source in sorted((SKILL_DIR / "commands" / "alla").glob("*.md")):
        text = source.read_text(encoding="utf-8")
        text = text if text.endswith("\n") else text + "\n"
        target = target_dir / source.name
        try:
            if target.is_file() and target.read_text(encoding="utf-8") == text:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as error:
            print("Не удалось установить {}: {}".format(target, error), flush=True)
            continue
        written.append(target)
    return written


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
                MIN_VERSION[0], MIN_VERSION[1], self_command("setup", "--python", "/path/to/python3.11")
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
    check = [str(python), "-m", "pip", "--version"]
    if subprocess.call(check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    print("В {} нет pip — ставлю его через ensurepip.".format(VENV_DIR), flush=True)
    if subprocess.call([str(python), "-m", "ensurepip", "--upgrade"]) == 0:
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


def setup(argv: Sequence[str]) -> int:
    explicit, pip_args = split_setup_args(argv)
    if VENV_DIR.exists():
        if not use_existing_venv(explicit):
            return 2
    else:
        code = create_venv(explicit)
        if code != 0:
            return code

    target = venv_python()
    if not ensure_pip(target):
        return 1
    gitignore = ensure_gitignored(PROJECT_DIR)
    if gitignore is not None:
        print("Добавил /.venv в {}".format(gitignore), flush=True)
    try:
        SETUP_MARKER.unlink()
    except FileNotFoundError:
        pass
    print(
        "Ставлю зависимости скилла из {} в .venv проекта {} (numpy, scipy, scikit-learn — "
        "это 1–3 минуты, не прерывай).".format(REQUIREMENTS, VENV_DIR),
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
    print("Зависимости скилла установлены в {}".format(VENV_DIR))
    created = create_env_file()
    if created is not None:
        print(
            "Создан {}: впиши ALLURE_ENDPOINT и ALLURE_TOKEN (токен в чат не пиши).".format(created)
        )
    if install_commands():
        print(
            "Установлены команды /alla:run, /alla:apply и другие в {}: Qwen Code покажет их "
            "после перезапуска.".format(PROJECT_DIR / ".qwen" / "commands" / "alla")
        )
    return 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(r"^\s*(\[\[?)\s*([^\[\]]*?)\s*\]\]?\s*(?:#.*)?$")
_STRING_RE = re.compile(
    r"""^\s*([A-Za-z_][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(?:#.*)?$"""
)
_URL_PASSWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]*:[^/@\s]*@")
_SKILL_NAME_RE = re.compile(r"^name:\s*alla-launch\s*$", re.MULTILINE)
UPDATE_CONFIG_HINT = (
    "Адрес репозитория вписывает в alla-extensions/config.toml проекта автотестов сам "
    "пользователь (скилл этот файл не правит):\n\n[update]\nrepo = \"git@git.example.com:team/alla.git\"\n"
    "ref = \"main\"                      # необязательно: ветка или тег\n"
    "path = \"qwen-skill/alla-launch\"   # необязательно: папка скилла в репозитории\n\n"
    "Разово адрес можно передать аргументом: update --source <URL>"
)


def read_update_config(project_dir: Path = PROJECT_DIR) -> dict[str, str]:
    """Строковые ключи таблицы ``[update]`` из ``alla-extensions/config.toml``.

    ``tomllib`` есть только с Python 3.11, а этот файл работает и на 3.8: читается
    ровно то, что нужно, — ``ключ = "значение"`` в одной таблице.
    """
    try:
        text = (project_dir / "alla-extensions" / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    in_update = False
    for line in text.splitlines():
        table = _TABLE_RE.match(line)
        if table:
            in_update = table.group(1) == "[" and table.group(2) == "update"
            continue
        match = _STRING_RE.match(line)
        if in_update and match:
            double, single = match.group(2), match.group(3)
            if double is not None:
                double = double.replace('\\"', '"').replace("\\\\", "\\")
            values[match.group(1)] = double if double is not None else single
    return values


def split_update_args(argv: Sequence[str]) -> tuple[str | None, str | None] | None:
    """``[--source URL] [--ref REF]`` → (URL, REF); ``None`` — неверные аргументы."""
    found: dict[str, str] = {}
    args = list(argv)
    while args:
        arg = args.pop(0)
        name, eq, value = arg.partition("=")
        if name not in ("--source", "--ref"):
            return None
        if not eq:
            if not args:
                return None
            value = args.pop(0)
        found[name] = value
    return found.get("--source"), found.get("--ref")


def git_env() -> dict[str, str]:
    """Окружение git без вопросов: ключ ssh и пароль спросить некому.

    Как ``git_env`` расширения ``sut-code``: его отсюда не импортировать.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env["GIT_ASKPASS"] = os.environ.get("GIT_ASKPASS", "echo")
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        try:
            configured = subprocess.run(
                ["git", "config", "--get", "core.sshCommand"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, universal_newlines=True,
                timeout=GIT_TIMEOUT,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            configured = ""
        if not configured:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def run_git(args: Sequence[str]) -> tuple[int, str]:
    """(код, вывод); 127 — git не запустился, 124 — не уложился в ``GIT_TIMEOUT``."""
    try:
        result = subprocess.run(
            ["git", *args], env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, universal_newlines=True, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, "git не ответил за {} с".format(GIT_TIMEOUT)
    except OSError as error:
        return 127, str(error)
    return result.returncode, result.stdout.strip()


def update_error(message: str, code: int = 1) -> int:
    print("STATUS: error")
    print(message)
    return code


def find_update_source(clone: Path, configured: str | None) -> tuple[Path | None, str]:
    """Папка скилла в клоне и текст ошибки, если её нет или это не alla-launch."""
    candidates = (configured,) if configured else UPDATE_SOURCE_PATHS
    for relative in candidates:
        folder = (clone / (relative or "")).resolve()
        if clone.resolve() not in (folder, *folder.parents):
            return None, "path = «{}» выходит за пределы репозитория.".format(relative)
        skill_md = folder / "SKILL.md"
        try:
            head = skill_md.read_text(encoding="utf-8")[:2000]
        except (OSError, UnicodeDecodeError):
            continue
        if not _SKILL_NAME_RE.search(head):
            continue
        for required in ("scripts/alla_skill.py", "requirements.txt"):
            if not (folder / required).is_file():
                return None, "В папке скилла репозитория нет {}.".format(required)
        return folder, ""
    where = "«{}»".format(configured) if configured else "qwen-skill/alla-launch или в корне"
    return None, "В репозитории нет скилла alla-launch (SKILL.md с name: alla-launch) в {}.".format(where)


def tree_files(root: Path) -> set[str]:
    found: set[str] = set()
    for folder, dirs, names in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git") and d not in UPDATE_KEEP]
        for name in names:
            if name.endswith(".pyc") or name in UPDATE_KEEP or name == VERSION_MARKER.name:
                continue
            found.add(Path(folder, name).relative_to(root).as_posix())
    return found


def tree_changes(old: Path, new: Path) -> tuple[int, int, int]:
    """(добавлено, изменено, удалено) файлов при замене ``old`` на ``new``."""
    before, after = tree_files(old), tree_files(new)
    changed = sum(
        1 for name in before & after if not filecmp.cmp(str(old / name), str(new / name), shallow=False)
    )
    return len(after - before), changed, len(before - after)


def swap_skill_dir(source: Path) -> str:
    """Заменить папку скилла копией ``source``; пустая строка — готово, иначе причина.

    Копия собирается рядом (одна файловая система) и ставится переименованием, старая
    папка сохраняется до конца: при сбое она возвращается на место.
    """
    staging = Path(tempfile.mkdtemp(prefix=".alla-update-", dir=str(SKILL_DIR.parent)))
    new_dir, old_dir = staging / "new", staging / "old"
    moved: list[str] = []
    swapped = False
    try:
        shutil.copytree(str(source), str(new_dir), ignore=UPDATE_IGNORE)
        os.rename(str(SKILL_DIR), str(old_dir))
        swapped = True
        for name in UPDATE_KEEP:
            kept = old_dir / name
            if os.path.lexists(str(kept)):
                os.rename(str(kept), str(new_dir / name))
                moved.append(name)
        os.rename(str(new_dir), str(SKILL_DIR))
    except (OSError, shutil.Error) as error:
        stuck: list[str] = []
        for name in moved:
            try:
                os.rename(str(new_dir / name), str(old_dir / name))
            except OSError:
                stuck.append(name)
        if swapped and not SKILL_DIR.exists():
            try:
                os.rename(str(old_dir), str(SKILL_DIR))
            except OSError:
                return "{}; прежняя папка скилла осталась в {}".format(error, old_dir)
        if stuck:
            # Единственная копия .env/.venv осталась в staging: не удалять.
            return "{}; {} не вернулись на место, они лежат в {}".format(
                error, ", ".join(stuck), new_dir
            )
        shutil.rmtree(str(staging), ignore_errors=True)
        return str(error)
    shutil.rmtree(str(staging), ignore_errors=True)
    return ""


def update(argv: Sequence[str]) -> int:
    args = split_update_args(argv)
    if args is None:
        return update_error(
            "Неверные аргументы update. Использование: {}".format(
                self_command("update", "[--source URL] [--ref REF]")
            ),
            2,
        )
    if not (SKILL_DIR.parent.name == "skills" and SKILL_DIR.parent.parent.name == ".qwen"):
        return update_error(
            "Скилл лежит в {}, а не в .qwen/skills/ проекта: это рабочая копия, update её не "
            "заменяет (пользователь обновляет такую копию через git сам).".format(SKILL_DIR),
            2,
        )
    config = read_update_config()
    source = args[0] or config.get("repo", "")
    ref = args[1] or config.get("ref", "")
    if not source:
        return update_error("Не задан репозиторий скилла. " + UPDATE_CONFIG_HINT, 2)
    if source.startswith("-") or ref.startswith("-"):
        return update_error("Адрес и ветка репозитория не могут начинаться с «-».", 2)
    if _URL_PASSWORD_RE.match(source):
        return update_error(
            "В адресе репозитория пароль или токен — так нельзя. Используй ssh-ключ или "
            "сохранённый пароль git (credential helper) и адрес без учётных данных.",
            2,
        )

    scratch = Path(tempfile.mkdtemp(prefix="alla-update-"))
    try:
        clone = scratch / "repo"
        # Первая строка stdout — STATUS, поэтому стадии идут в stderr.
        print(
            "Получаю скилл из {}{}".format(source, " ({})".format(ref) if ref else ""),
            file=sys.stderr, flush=True,
        )
        clone_args = ["-c", "protocol.ext.allow=never", "clone", "--quiet", "--depth", "1"]
        if ref:
            clone_args += ["--branch", ref]
        code, output = run_git(clone_args + ["--", source, str(clone)])
        if code != 0:
            return update_error(
                "Не удалось получить репозиторий {} (git, код {}):\n{}\nДоступ — ключом ssh или "
                "сохранённым паролем git; корпоративный сертификат — в настройках git "
                "(http.sslCAInfo).".format(source, code, "\n".join(output.splitlines()[-5:]))
            )
        folder, problem = find_update_source(clone, config.get("path") or None)
        if folder is None:
            return update_error(problem + " Скилл не изменён.")
        code, sha = run_git(["-C", str(clone), "rev-parse", "HEAD"])
        if code != 0 or not sha:
            return update_error("Не удалось определить версию репозитория: {}".format(sha))
        # Версия — коммит и папка скилла в нём: смена path при том же коммите — другая версия.
        version = "{} {}".format(sha, folder.resolve().relative_to(clone.resolve()).as_posix())

        try:
            current = VERSION_MARKER.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current == version:
            print("STATUS: ok")
            print("Скилл уже последней версии ({}), ничего не менял.".format(sha[:8]))
            return 0
        added, changed, removed = tree_changes(SKILL_DIR, folder)
        reason = swap_skill_dir(folder)
        if reason:
            return update_error("Не удалось заменить папку скилла: {}. Скилл не изменён.".format(reason))
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)

    try:
        VERSION_MARKER.write_text(version + "\n", encoding="utf-8")
    except OSError as error:
        print("Не удалось записать {}: {}".format(VERSION_MARKER, error), file=sys.stderr, flush=True)
    print("STATUS: updated")
    print(
        "Скилл обновлён: {} → {}. Файлов: добавлено {}, изменено {}, удалено {}.".format(
            current[:8] or "версия не была записана", sha[:8], added, changed, removed
        )
    )
    if install_commands():
        print("Команды /alla:* обновлены: Qwen Code покажет их после перезапуска.")
    if not setup_complete():
        print(
            "Зависимости нужно доустановить (изменился requirements.txt или окружения ещё нет); "
            "команда займёт 1–3 минуты:"
        )
        print(self_command("setup"))
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
            "Зависимости скилла в .venv проекта ({}) {}. Выполни один раз (займёт пару "
            "минут):".format(VENV_DIR, state)
        )
        print(self_command("setup"))
        print("Затем повтори команду:")
        print(self_command(*argv))
        return 0  # статус с инструкцией, а не авария: агент должен выполнить setup

    if running_in_venv() or os.environ.get(CHILD_MARKER):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from alla_skill_lib.cli import main as cli_main

        return cli_main(list(argv))

    env = dict(os.environ, **{CHILD_MARKER: "1"})
    command = [str(venv_python()), str(Path(__file__).resolve())] + list(argv)
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
