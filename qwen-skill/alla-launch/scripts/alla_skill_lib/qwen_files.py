"""Файлы скилла, которые Qwen Code ищет в проекте: субагенты и слэш-команды ``/alla:*``.

Qwen Code читает агентов из ``<проект>/.qwen/agents/`` и команды из
``<проект>/.qwen/commands/`` при старте сеанса, а скилл лежит в ``.qwen/skills/`` —
поэтому файлы приходится копировать туда. Скилл делает это только командой
``install-files --yes`` после согласия пользователя, ``setup``, ``update`` и ``prepare``
в ``.qwen/`` ничего не пишут. Только что установленное Qwen увидит со следующего сеанса.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from alla_skill_lib import workspace as ws
from alla_skill_lib.batch_task import BATCH_AGENT

# Подпапка даёт командам префикс: commands/alla/apply.md → /alla:apply.
COMMANDS_NAMESPACE = "alla"

logger = logging.getLogger(__name__)


def batch_agent_source() -> Path:
    """Описание субагента в папке скилла (``ws.SKILL_DIR`` читается при вызове — его подменяют тесты)."""
    return ws.SKILL_DIR / "agents" / f"{BATCH_AGENT}.md"


def commands_source() -> Path:
    return ws.SKILL_DIR / "commands" / COMMANDS_NAMESPACE


def _install(source: Path, target: Path) -> bool:
    """Скопировать ``source`` в ``target``, если его там нет или он другой; True — записан."""
    text = _written_text(source)
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(target, text)
    except OSError as error:
        logger.warning("Не удалось установить %s: %s", target, error)
        return False
    return True


@dataclass(frozen=True)
class PlannedFile:
    """Один файл скилла для ``.qwen/`` проекта и что с ним сделает установка."""

    what: str  # «команда /alla:run», «субагент пакетов alla-batch»
    source: Path
    target: Path
    state: str  # NEW | CHANGED | SAME


NEW, CHANGED, SAME = "new", "changed", "same"


def _written_text(source: Path) -> str:
    """Текст, каким ``ws.write_text`` запишет файл: всегда с переводом строки в конце."""
    text = source.read_text(encoding="utf-8")
    return text if text.endswith("\n") else text + "\n"


def _state(source: Path, target: Path) -> str:
    try:
        if not target.is_file():
            return NEW
        return SAME if target.read_text(encoding="utf-8") == _written_text(source) else CHANGED
    except OSError:
        return CHANGED


def planned_files(
    project_root: Path,
    extension_agents: Mapping[str, Path] | None = None,
    extension_commands: Mapping[str, tuple[str, Path]] | None = None,
) -> list[PlannedFile]:
    """Файлы, которые ``install-files`` положит в ``<проект>/.qwen/``; на диск ничего не пишет.

    Субагент пакетов ``alla-batch``, команды ``/alla:*`` скилла и — для включённых расширений —
    их субагенты (``extension_agents``: имя агента → файл) и команды
    (``extension_commands``: имя команды → (имя расширения, файл)). Лишние файлы в папках
    пользователя не затрагиваются.
    """
    files: list[PlannedFile] = []
    agent = batch_agent_source()
    if agent.is_file():
        target = extension_agent_target(project_root, BATCH_AGENT)
        files.append(PlannedFile(f"субагент пакетов {BATCH_AGENT}", agent, target, _state(agent, target)))
    for source in sorted(commands_source().glob("*.md")):
        target = extension_command_target(project_root, source.stem)
        files.append(PlannedFile(f"команда /alla:{source.stem}", source, target, _state(source, target)))
    for name, source in (extension_agents or {}).items():
        target = extension_agent_target(project_root, name)
        files.append(PlannedFile(f"субагент расширения {name}", source, target, _state(source, target)))
    for name, (owner, source) in (extension_commands or {}).items():
        target = extension_command_target(project_root, name)
        files.append(
            PlannedFile(f"команда /alla:{name} расширения {owner}", source, target, _state(source, target))
        )
    return files


def install_planned(files: Sequence[PlannedFile]) -> list[PlannedFile]:
    """Записать новые и изменившиеся файлы плана; возвращает записанные."""
    return [item for item in files if item.state != SAME and _install(item.source, item.target)]


def extension_agent_target(project_root: Path, agent_name: str) -> Path:
    return project_root / ".qwen" / "agents" / f"{agent_name}.md"


def install_extension_agent(project_root: Path, source: Path, agent_name: str) -> Path | None:
    """Положить субагента расширения в ``<проект>/.qwen/agents/<имя агента>.md``.

    Как у субагента пакетов: Qwen увидит его со следующего сеанса. Возвращает путь, если
    файл записан.
    """
    target = extension_agent_target(project_root, agent_name)
    return target if _install(source, target) else None


def remove_extension_agent(project_root: Path, source: Path | None, agent_name: str) -> str:
    """Убрать субагента выключенного расширения: ``removed`` | ``absent`` | ``kept`` (файл
    правили руками — не наш, не удаляем)."""
    return _remove_ours(extension_agent_target(project_root, agent_name), source)


def extension_command_target(project_root: Path, name: str) -> Path:
    return project_root / ".qwen" / "commands" / COMMANDS_NAMESPACE / f"{name}.md"


def install_extension_command(project_root: Path, source: Path, name: str) -> Path | None:
    """Положить слэш-команду расширения в ``.qwen/commands/alla/<имя>.md`` (``/alla:<имя>``).

    Qwen увидит её со следующего сеанса. Возвращает путь, если файл записан.
    """
    target = extension_command_target(project_root, name)
    return target if _install(source, target) else None


def remove_extension_command(project_root: Path, source: Path, name: str) -> str:
    """Убрать команду выключенного расширения, только если файл — наш (как у субагента)."""
    return _remove_ours(extension_command_target(project_root, name), source)


def _remove_ours(target: Path, source: Path | None) -> str:
    if not target.is_file():
        return "absent"
    try:
        same = source is not None and target.read_text(encoding="utf-8") == source.read_text(
            encoding="utf-8"
        )
        if not same:
            return "kept"
        target.unlink()
    except OSError as error:
        logger.warning("Не удалось убрать %s: %s", target, error)
        return "kept"
    return "removed"
