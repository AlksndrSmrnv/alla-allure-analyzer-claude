"""Расширения скилла: свои функции команды без форка и правки кода скилла.

Расширение — папка с манифестом ``extension.toml``: встроенные лежат в
``<скилл>/extensions/``, свои — в ``<проект>/alla-extensions/`` (своё с тем же именем
заменяет встроенное). Включаются строкой ``enabled = [...]`` в
``<проект>/alla-extensions/config.toml``.

На событие скилла (``after_apply``, ``after_revert``, ``after_report``) расширение
отвечает хуком:

* ``task`` — задание своему субагенту: скилл пишет контекст и задание в папку разбора, а
  в ответ команды добавляет блок «Расширения:» с точной инструкцией основному агенту;
* ``script`` — скрипт, который скилл запускает сам; его вывод уходит пользователю.

Код расширения скилл не импортирует: скрипты идут отдельным процессом и получают данные
через JSON-контекст (``ALLA_CONTEXT``) с версией контракта ``api``. Ошибка расширения не
ломает команду скилла — она становится строкой блока «Расширения:».
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import Template
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.batch_task import BATCH_AGENT
from alla_skill_lib.proposals import parse_proposal

API_VERSION = 1
EVENTS = ("after_apply", "after_revert", "after_report")
PROBLEM_EVENTS = ("after_apply", "after_revert")
PROJECT_DIRNAME = "alla-extensions"
CONFIG_FILE = "config.toml"
MANIFEST_FILE = "extension.toml"
AGENT_PREFIX = "alla-"
BLOCK_HEADING = "Расширения:"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
# Имена, чей субагент alla-<имя> затёр бы субагента самого скилла в .qwen/agents/.
RESERVED_NAMES = frozenset({BATCH_AGENT.removeprefix("alla-")})
ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
DEFAULT_TIMEOUT = 120  # секунд на скрипт расширения
MAX_TIMEOUT = 600
OUTPUT_LIMIT = 4000  # символов вывода скрипта в ответе скилла
_ENABLED_LINE_RE = re.compile(r"^enabled\s*=\s*\[[^\]\n]*\]\s*(?:#.*)?$", re.MULTILINE)
_ENABLED_KEY_RE = re.compile(r"^enabled\s*=", re.MULTILINE)
_SETTING_TYPES = (str, bool, int)
CONFIG_HEADER = (
    "# Расширения скилла alla-launch для этого проекта.\n"
    "# enabled — включённые расширения; меняют его команды\n"
    "#   alla_skill.py extensions enable <имя> / disable <имя>.\n"
    "# Настройки расширения — таблица с его именем, например:\n"
    "# [git-publish]\n"
    "# remote = \"origin\"\n"
)


class ExtensionError(Exception):
    """Расширение или его настройка не годятся; текст — для пользователя."""


@dataclass(frozen=True)
class Hook:
    event: str
    task: Path | None = None
    script: Path | None = None
    ask: str = ""  # вопрос пользователю перед запуском субагента (при confirm)


@dataclass(frozen=True)
class Extension:
    name: str
    title: str
    description: str
    root: Path
    bundled: bool
    hooks: tuple[Hook, ...]
    actions: dict[str, Path]
    settings: dict[str, Any]
    agent: Path | None = None
    timeout: int = DEFAULT_TIMEOUT
    overrides_bundled: bool = False

    @property
    def agent_name(self) -> str:
        return AGENT_PREFIX + self.name

    def hooks_for(self, event: str) -> list[Hook]:
        return [hook for hook in self.hooks if hook.event == event]


@dataclass
class Catalog:
    """Найденные расширения и ошибки манифестов (``имя: что не так``)."""

    extensions: dict[str, Extension] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    enabled: tuple[str, ...] = ()
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# обнаружение и манифест
# ---------------------------------------------------------------------------


def bundled_dir() -> Path:
    """Встроенные расширения (``ws.SKILL_DIR`` читается при вызове — его подменяют тесты)."""
    return ws.SKILL_DIR / "extensions"


def project_dir(project_root: Path) -> Path:
    return project_root / PROJECT_DIRNAME


def config_path(project_root: Path) -> Path:
    return project_dir(project_root) / CONFIG_FILE


def discover(project_root: Path) -> Catalog:
    catalog = Catalog()
    for bundled, base in ((True, bundled_dir()), (False, project_dir(project_root))):
        if not base.is_dir():
            continue
        for folder in _extension_folders(base, bundled=bundled):
            try:
                extension = load_manifest(folder, bundled=bundled)
            except (ExtensionError, OSError, ValueError) as exc:  # ValueError — и UnicodeDecodeError
                error = str(exc) if isinstance(exc, ExtensionError) else f"{type(exc).__name__}: {exc}"
                if not bundled and catalog.extensions.pop(folder.name, None) is not None:
                    # Команда заменила встроенное своим (например, с ограничениями git-publish):
                    # неисправная замена выключает расширение, а не возвращает встроенное.
                    error = f"своя замена встроенного расширения не работает — оно выключено: {error}"
                catalog.errors[folder.name] = error
                continue
            if not bundled and extension.name in catalog.extensions:
                extension = replace(extension, overrides_bundled=True)
            catalog.extensions[extension.name] = extension
            catalog.errors.pop(extension.name, None)
    return catalog


def _extension_folders(base: Path, *, bundled: bool) -> list[Path]:
    """Папки расширений. В ``alla-extensions/`` расширение — любая папка, и папка без
    манифеста — неисправное расширение: иначе удалённый ``extension.toml`` своей замены
    молча вернул бы встроенное (с его настройками вместо ограничений команды)."""
    folders = [path for path in base.iterdir()
               if path.is_dir() and not path.name.startswith((".", "_"))]
    if bundled:
        folders = [path for path in folders if (path / MANIFEST_FILE).is_file()]
    return sorted(folders)


def load_manifest(folder: Path, *, bundled: bool) -> Extension:
    if not (folder / MANIFEST_FILE).is_file():
        raise ExtensionError(f"в папке нет файла {MANIFEST_FILE}")
    try:
        data = tomllib.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionError(f"{MANIFEST_FILE} не читается: {exc}") from exc
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ExtensionError("name — латиница в нижнем регистре, цифры и дефис, с буквы")
    if name in RESERVED_NAMES:
        raise ExtensionError(f"имя «{name}» занято самим скиллом (субагент alla-{name})")
    if name != folder.name:
        raise ExtensionError(f"name «{name}» не совпадает с именем папки «{folder.name}»")
    if data.get("api") != API_VERSION:
        raise ExtensionError(
            f"api = {data.get('api')!r}: эта версия скилла поддерживает только api = {API_VERSION}"
        )
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ExtensionError("[settings] — таблица значений")
    for key, value in settings.items():
        if not _valid_setting(value):
            raise ExtensionError(f"настройка {key}: строка, число, да/нет или список строк")
    settings.setdefault("confirm", False)
    if not isinstance(settings["confirm"], bool):
        raise ExtensionError("настройка confirm — true или false")
    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
        raise ExtensionError(f"timeout — целое число секунд от 1 до {MAX_TIMEOUT}")
    agent = _file(folder, data["agent"], "agent") if "agent" in data else None
    if agent is not None:
        _check_agent(agent, AGENT_PREFIX + name)
    hooks = tuple(_hook(folder, raw, agent) for raw in _list_of_tables(data, "hooks"))
    task_events = [hook.event for hook in hooks if hook.task is not None]
    repeated = sorted({event for event in task_events if task_events.count(event) > 1})
    if repeated:
        # Задание события пишется в один файл <событие>-NN.md: второе затёрло бы первое.
        raise ExtensionError(
            f"хук task на событие {', '.join(repeated)} — не больше одного: шаги одного "
            "субагента пишутся в одно задание"
        )
    actions_raw = data.get("actions", {})
    if not isinstance(actions_raw, dict):
        raise ExtensionError("[actions] — таблица «имя = путь к скрипту»")
    actions: dict[str, Path] = {}
    for action, script in actions_raw.items():
        if not ACTION_RE.fullmatch(action):
            raise ExtensionError(f"действие «{action}»: латиница, цифры, дефис и _, с буквы")
        actions[action] = _file(folder, script, f"actions.{action}")
    if not hooks and not actions:
        raise ExtensionError("нет ни хуков [[hooks]], ни действий [actions] — расширение ничего не делает")
    return Extension(
        name=name,
        title=str(data.get("title") or name),
        description=str(data.get("description") or ""),
        root=folder.resolve(),
        bundled=bundled,
        hooks=hooks,
        actions=actions,
        settings=settings,
        agent=agent,
        timeout=timeout,
    )


def _valid_setting(value: Any) -> bool:
    if isinstance(value, list):
        return all(isinstance(item, str) for item in value)
    return isinstance(value, _SETTING_TYPES)


def _list_of_tables(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ExtensionError(f"{key} — список таблиц [[{key}]]")
    return raw


def _file(folder: Path, raw: Any, where: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ExtensionError(f"{where} — путь к файлу внутри папки расширения")
    path = (folder / raw).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ExtensionError(f"{where}: файл {raw} вне папки расширения")
    if not path.is_file():
        raise ExtensionError(f"{where}: нет файла {raw}")
    return path


def _hook(folder: Path, raw: dict[str, Any], agent: Path | None) -> Hook:
    event = raw.get("event")
    if event not in EVENTS:
        raise ExtensionError(f"событие {event!r} неизвестно; есть: {', '.join(EVENTS)}")
    if ("task" in raw) == ("script" in raw):
        raise ExtensionError(f"хук {event}: нужен ровно один из ключей task или script")
    if "task" in raw:
        if agent is None:
            raise ExtensionError(f"хук {event} с task: в манифесте нет agent — субагента расширения")
        return Hook(event, task=_file(folder, raw["task"], f"{event}.task"), ask=str(raw.get("ask", "")))
    return Hook(event, script=_file(folder, raw["script"], f"{event}.script"))


def _check_agent(path: Path, expected: str) -> None:
    """Файл агента Qwen: невалидный Qwen пропускает молча, и хук ушёл бы в пустоту."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExtensionError(f"{path.name} не читается: {exc}") from exc
    parts = text.split("---\n", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ExtensionError(f"{path.name}: нет заголовка --- name/description/tools ---")
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            fields[key.strip()] = value.strip()
    if fields.get("name") != expected:
        raise ExtensionError(f"{path.name}: name субагента должен быть {expected}")
    if not fields.get("description"):
        raise ExtensionError(f"{path.name}: нет description субагента")
    if len(parts[2].strip()) < 10:
        raise ExtensionError(f"{path.name}: пустая подсказка субагента")


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------


def load_config(project_root: Path) -> Config:
    path = config_path(project_root)
    if not path.is_file():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionError(f"{path} не читается: {exc}") from exc
    enabled = data.get("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(name, str) for name in enabled):
        raise ExtensionError(f"{path}: enabled — список имён расширений")
    settings = {key: value for key, value in data.items() if isinstance(value, dict)}
    return Config(tuple(dict.fromkeys(enabled)), settings)


def set_enabled(project_root: Path, name: str, on: bool) -> bool:
    """Включить или выключить расширение; True — ``config.toml`` изменён.

    Переписывается только строка ``enabled = [...]``: комментарии и настройки остаются.
    """
    config = load_config(project_root)
    names = [item for item in config.enabled if item != name] + ([name] if on else [])
    if list(config.enabled) == names:
        return False
    line = "enabled = [" + ", ".join(json.dumps(item) for item in names) + "]"
    path = config_path(project_root)
    text = path.read_text(encoding="utf-8") if path.is_file() else CONFIG_HEADER
    if _ENABLED_LINE_RE.search(text):
        text = _ENABLED_LINE_RE.sub(lambda _: line, text, count=1)
    elif _ENABLED_KEY_RE.search(text):
        raise ExtensionError(
            f"В {path} список enabled записан в несколько строк — его не переписать "
            f"автоматически. Запиши его одной строкой: {line}"
        )
    else:
        table = re.search(r"^\[", text, re.MULTILINE)
        cut = table.start() if table else len(text)
        head = text[:cut]
        if head and not head.endswith("\n"):
            head += "\n"
        text = head + line + "\n" + ("\n" + text[cut:] if table else "")
    ws.write_atomic(path, text)
    return True


def resolved_settings(extension: Extension, config: Config) -> dict[str, Any]:
    """Настройки манифеста с переопределениями из ``config.toml``; неверные — ``ExtensionError``."""
    settings = dict(extension.settings)
    for key, value in config.settings.get(extension.name, {}).items():
        if key not in settings:
            raise ExtensionError(
                f"в {CONFIG_FILE} [{extension.name}] неизвестная настройка {key}; есть: "
                + ", ".join(sorted(settings))
            )
        default = settings[key]
        if type(value) is not type(default) or not _valid_setting(value):
            raise ExtensionError(
                f"в {CONFIG_FILE} [{extension.name}] {key} — нужен тот же тип, что {default!r}"
            )
        settings[key] = value
    return settings


@dataclass
class Active:
    """Включённые расширения проекта, готовые к работе, и что с остальными не так."""

    extensions: list[tuple[Extension, dict[str, Any]]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def active(project_root: Path) -> Active:
    result = Active()
    try:
        config = load_config(project_root)
    except ExtensionError as exc:
        result.problems.append(str(exc))
        return result
    if not config.enabled:
        return result
    try:
        catalog = discover(project_root)
    except Exception as exc:  # prepare, next и apply не должны падать из-за папки расширений
        result.problems.append(f"расширения не прочитаны: {type(exc).__name__}: {exc}")
        return result
    for name in config.enabled:
        extension = catalog.extensions.get(name)
        if extension is None:
            reason = catalog.errors.get(name, "не найдено")
            result.problems.append(f"расширение {name} включено, но не работает: {reason}")
            continue
        try:
            result.extensions.append((extension, resolved_settings(extension, config)))
        except ExtensionError as exc:
            result.problems.append(f"расширение {name}: {exc}")
    return result


def find_active(project_root: Path, name: str) -> tuple[Extension, dict[str, Any]]:
    current = active(project_root)
    for extension, settings in current.extensions:
        if extension.name == name:
            return extension, settings
    reason = next((problem for problem in current.problems if f" {name}" in problem), "")
    raise ExtensionError(reason or f"расширение {name} не включено в {config_path(project_root)}")


# ---------------------------------------------------------------------------
# события
# ---------------------------------------------------------------------------


def out_dir(paths: ws.RunPaths, extension: Extension) -> Path:
    return paths.root / "extensions" / extension.name


def problem_context(
    paths: ws.RunPaths,
    entry: dict[str, Any],
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Данные проблемы для расширения: метка, разбор, предложение правки, запись apply."""
    file_id = entry["file_id"]
    analysis_path = paths.analysis(file_id)
    analysis = parse_analysis(ws.read_text(analysis_path)) if analysis_path.is_file() else None
    proposal_path = paths.proposal(file_id)
    proposal = parse_proposal(ws.read_text(proposal_path)) if proposal_path.is_file() else None
    return {
        "number": int(file_id),
        "file_id": file_id,
        "label": " ".join(str(entry.get("label", "")).split()),
        "category": analysis.category if analysis else None,
        "cause": analysis.cause if analysis else "",
        "fix": analysis.fix if analysis else "",
        "analysis_path": str(analysis_path),
        "proposal_path": str(proposal_path),
        "patch_path": str(paths.proposal_patch(file_id)),
        "proposal": {
            "file": proposal.file if proposal else None,
            "line": proposal.line if proposal else None,
            "why": proposal.why if proposal else "",
        },
        "record": record,
    }


def fire(
    event: str,
    paths: ws.RunPaths,
    run: dict[str, Any],
    data: dict[str, Any],
    *,
    only: Collection[str] | None = None,
) -> list[str]:
    """Выполнить хуки события; строки блока «Расширения:» (пусто — расширений нет).

    ``data`` — поля события: ``problem`` для событий проблемы, ``console`` и
    ``problems`` для ``after_report``. ``only`` — хуки только этих расширений.
    """
    project_root = Path(run["project_root"])
    current = active(project_root)
    lines = problem_lines(current.problems) if only is None else []
    for extension, settings in current.extensions:
        if only is not None and extension.name not in only:
            continue
        for hook in extension.hooks_for(event):
            try:
                lines += _fire_hook(extension, settings, hook, paths, run, data)
            except Exception as exc:  # расширение не должно ронять команду скилла
                lines.append(
                    f"- {extension.title} ({extension.name}): не сработало — {type(exc).__name__}: "
                    f"{exc}. Покажи пользователю эту строку."
                )
    return lines


def problem_lines(problems: list[str]) -> list[str]:
    """Строки блока «Расширения:» о неисправных включённых расширениях."""
    return [f"- Расширения не работают: {problem}. Покажи пользователю эту строку."
            for problem in problems]


def block(lines: list[str]) -> str:
    """Блок для ответа команды: пусто, если расширениям нечего сказать."""
    return "\n".join([BLOCK_HEADING, *lines]) if lines else ""


def _fire_hook(
    extension: Extension,
    settings: dict[str, Any],
    hook: Hook,
    paths: ws.RunPaths,
    run: dict[str, Any],
    data: dict[str, Any],
) -> list[str]:
    directory = out_dir(paths, extension)
    directory.mkdir(parents=True, exist_ok=True)
    problem = data.get("problem")
    stem = hook.event + (f"-{problem['file_id']}" if problem else "")
    context_file = directory / f"{stem}.json"
    context = {
        "api": API_VERSION,
        "event": hook.event,
        "extension": extension.name,
        "launch_id": run.get("launch_id"),
        "launch_name": run.get("launch_name"),
        "run_dir": str(paths.root),
        "project_root": str(run["project_root"]),
        "report_path": str(paths.report),
        "out_dir": str(directory),
        "stem": stem,
        "settings": settings,
        **data,
    }
    ws.write_json(context_file, context)
    label = f"{extension.title} ({extension.name})"
    if hook.script is not None:
        code, output = run_script(extension, hook.script, context_file, Path(run["project_root"]))
        if code != 0:
            return [f"- {label}: не сработало ({output or f'код {code}'}). Покажи пользователю эту строку."]
        if not output:
            return []
        return [f"- {label} — покажи пользователю как есть:", *_indent(output)]
    assert hook.task is not None
    task_file = directory / f"{stem}.md"
    ws.write_text(task_file, render_task(extension, hook.task, context, context_file))
    launch = (
        f"запусти субагента инструментом agent: subagent_type «{extension.agent_name}», "
        f"run_in_background: false, prompt дословно: «Выполни задание {task_file}». Нет такого "
        "типа субагентов — тот же вызов без subagent_type. Ответ субагента покажи пользователю "
        "как есть, кроме строк «Проблема скилла: …»."
    )
    if settings.get("confirm"):
        question = hook.ask or f"запустить «{extension.title}»"
        return [f"- {label}: спроси пользователя: «{question}» Только после явного «да» {launch}"]
    return [f"- {label}: {launch}"]


def _indent(text: str) -> list[str]:
    return [f"  {line}" if line else "" for line in text.splitlines()]


def render_task(
    extension: Extension,
    template: Path,
    context: dict[str, Any],
    context_file: Path,
) -> str:
    """Задание субагенту: шаблон расширения с данными события (``$имя``)."""
    problem = context.get("problem") or {}
    proposal = problem.get("proposal") or {}
    values: dict[str, Any] = {
        "context": context_file,
        "out_dir": context["out_dir"],
        "stem": context["stem"],
        "run_dir": context["run_dir"],
        "project_root": context["project_root"],
        "report_path": context["report_path"],
        "launch_id": context.get("launch_id") or "",
        "problem": problem.get("number", ""),
        "label": problem.get("label", ""),
        "category": problem.get("category") or "",
        "cause": problem.get("cause", ""),
        "fix": problem.get("fix", ""),
        "file": proposal.get("file") or "",
        "line": proposal.get("line") or "",
        "why": proposal.get("why", ""),
        "patch_path": problem.get("patch_path", ""),
    }
    for action in extension.actions:
        values[f"action_{action.replace('-', '_')}"] = ws.skill_command(
            "extensions", "action", extension.name, action, "--context", str(context_file)
        )
    for key, value in context["settings"].items():
        values[f"settings_{key.replace('-', '_')}"] = value
    text = template.read_text(encoding="utf-8")
    # Тексты разбора — одной строкой: перевод строки в них сломал бы разметку задания.
    return Template(text).safe_substitute({key: " ".join(str(value).split())
                                           for key, value in values.items()})


# ---------------------------------------------------------------------------
# скрипты расширения
# ---------------------------------------------------------------------------


def run_script(
    extension: Extension,
    script: Path,
    context_file: Path,
    project_root: Path,
    args: list[str] | None = None,
) -> tuple[int, str]:
    """Запустить скрипт расширения отдельным процессом; (код возврата, вывод)."""
    env = {
        **os.environ,
        "ALLA_CONTEXT": str(context_file),
        "ALLA_EXTENSION_DIR": str(extension.root),
        "ALLA_PROJECT_ROOT": str(project_root),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",  # git не должен ждать пароль, которого никто не введёт
    }
    try:
        result = subprocess.run(
            [sys.executable, str(script), *(args or [])],
            cwd=project_root,
            env=env,
            capture_output=True,
            timeout=extension.timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"скрипт {script.name} не уложился в {extension.timeout} с"
    except OSError as exc:
        return 126, f"скрипт {script.name} не запустился: {exc}"
    output = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0 and not output:
        error = result.stderr.decode("utf-8", "replace").strip().splitlines()
        output = error[-1] if error else ""
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + "\n… (вывод обрезан)"
    return result.returncode, output


def run_action(
    project_root: Path,
    reports_dir: Path,
    name: str,
    action: str,
    context: str,
    args: list[str],
) -> tuple[str, str]:
    """Действие расширения (``extensions action``): статус и текст ответа."""
    extension, _ = find_active(project_root, name)
    script = extension.actions.get(action)
    if script is None:
        raise ExtensionError(
            f"у расширения {name} нет действия {action}; есть: {', '.join(extension.actions) or 'нет'}"
        )
    # Контекст пишет только скилл, в папку разбора: чужой файл подсунул бы скрипту чужие данные.
    context_file = Path(context).expanduser().resolve()
    reports = reports_dir.resolve()
    if (
        not context_file.is_file()
        or context_file.suffix != ".json"
        or len(context_file.parents) < 4
        or context_file.parents[2].parent != reports
        or context_file.parent.name != name
        or context_file.parent.parent.name != "extensions"
    ):
        raise ExtensionError(
            f"контекст {context} — не файл расширения {name} из папки разбора "
            f"({reports}/<разбор>/extensions/{name}/…json)"
        )
    code, output = run_script(extension, script, context_file, project_root, args)
    first, _, rest = output.partition("\n")
    status = first.removeprefix("STATUS:").strip() if first.startswith("STATUS:") else ""
    if status not in ("ok", "error"):
        return "error", (
            f"Расширение {name} ({action}) ответило без строки STATUS: ok|error"
            f" (код {code}):\n{output}"
        )
    if code != 0 and status == "ok":
        status = "error"
    return status, rest.strip()
