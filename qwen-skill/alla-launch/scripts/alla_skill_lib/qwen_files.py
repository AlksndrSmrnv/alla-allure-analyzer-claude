"""Файлы скилла, которые Qwen Code ищет в проекте: субагенты и слэш-команды ``/alla:*``.

Qwen Code читает агентов из ``<проект>/.qwen/agents/`` и команды из
``<проект>/.qwen/commands/`` при старте сеанса, а скилл лежит в ``.qwen/skills/`` —
поэтому скилл сам копирует их туда. Только что установленное Qwen увидит со
следующего сеанса.
"""

from __future__ import annotations

import logging
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
    text = source.read_text(encoding="utf-8")
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(target, text)
    except OSError as error:
        logger.warning("Не удалось установить %s: %s", target, error)
        return False
    return True


def install_batch_agent(project_root: Path) -> Path | None:
    """Положить описание субагента в ``<проект>/.qwen/agents/``, если его нет или оно другое.

    В текущем сеансе ``agent`` ответит, что такого типа нет (на это есть запасной путь в
    ответе ``next``). Возвращает путь, если файл записан.
    """
    source = batch_agent_source()
    if not source.is_file():
        return None
    target = project_root / ".qwen" / "agents" / source.name
    return target if _install(source, target) else None


def install_commands(project_root: Path) -> list[Path]:
    """Положить команды ``/alla:*`` в ``<проект>/.qwen/commands/alla/``; возвращает записанные.

    Лишние файлы в этой папке не удаляются: их мог добавить пользователь.
    Та же логика — ``install_commands`` в ``alla_skill.py`` (``setup`` идёт без этой библиотеки).
    """
    source_dir = commands_source()
    target_dir = project_root / ".qwen" / "commands" / COMMANDS_NAMESPACE
    return [
        target_dir / source.name
        for source in sorted(source_dir.glob("*.md"))
        if _install(source, target_dir / source.name)
    ]


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
