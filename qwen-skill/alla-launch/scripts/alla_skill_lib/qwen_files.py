"""Файлы скилла, которые Qwen Code ищет в проекте: субагенты и слэш-команды ``/alla:*``.

Qwen Code читает агентов из ``<проект>/.qwen/agents/`` и команды из
``<проект>/.qwen/commands/`` при старте сеанса, а скилл лежит в ``.qwen/skills/`` —
поэтому инженер копирует туда готовые файлы. ``install-files`` только печатает
пути, содержимое и diff; скилл не устанавливает и не удаляет эти файлы. Только что установленное Qwen увидит со следующего сеанса.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from alla_skill_lib import workspace as ws
from alla_skill_lib.batch_task import BATCH_AGENT

# Подпапка даёт командам префикс: commands/alla/apply.md → /alla:apply.
COMMANDS_NAMESPACE = "alla"

def batch_agent_source() -> Path:
    """Описание субагента в папке скилла (``ws.SKILL_DIR`` читается при вызове — его подменяют тесты)."""
    return ws.SKILL_DIR / "agents" / f"{BATCH_AGENT}.md"


def commands_source() -> Path:
    return ws.SKILL_DIR / "commands" / COMMANDS_NAMESPACE


@dataclass(frozen=True)
class PlannedFile:
    """Один файл скилла для ``.qwen/`` проекта и его состояние в проекте."""

    what: str  # «команда /alla:run», «субагент пакетов alla-batch»
    source: Path
    target: Path
    state: str  # NEW | CHANGED | SAME


NEW, CHANGED, SAME = "new", "changed", "same"


def _written_text(source: Path) -> str:
    """Готовый текст файла: всегда с переводом строки в конце."""
    text = source.read_text(encoding="utf-8")
    return text if text.endswith("\n") else text + "\n"


def _state(source: Path, target: Path) -> str:
    try:
        if not target.is_file():
            return NEW
        return SAME if target.read_text(encoding="utf-8") == _written_text(source) else CHANGED
    except (OSError, ValueError):  # ValueError — файл не в UTF-8: заменить
        return CHANGED


def planned_files(
    project_root: Path,
    extension_agents: Mapping[str, Path] | None = None,
    extension_commands: Mapping[str, tuple[str, Path]] | None = None,
) -> list[PlannedFile]:
    """Файлы, которые инженер размещает в ``<проект>/.qwen/``; на диск ничего не пишет.

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


def extension_agent_target(project_root: Path, agent_name: str) -> Path:
    return project_root / ".qwen" / "agents" / f"{agent_name}.md"


def extension_command_target(project_root: Path, name: str) -> Path:
    return project_root / ".qwen" / "commands" / COMMANDS_NAMESPACE / f"{name}.md"
