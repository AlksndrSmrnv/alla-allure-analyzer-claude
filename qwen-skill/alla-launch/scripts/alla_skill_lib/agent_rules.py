"""Правила исполнителя и ссылки на справочники — общий текст для всех заданий.

Агент, который ведёт разбор, — исполнитель сценария скилла: он не меняет сам
скилл, не пишет свои скрипты и сообщает о неполадках скилла вместо обхода.
Эти правила есть в ``SKILL.md``, но после сжатия контекста или у субагента,
который видит только свой файл задания, ``SKILL.md`` недоступен. Поэтому один и
тот же текст (``EXECUTOR_RULES``) подставляется в ``clusters/NN.md``,
``batches/N.md`` и ``summary_task.md``; ``tests/test_skill_docs.py`` сверяет его
с ``SKILL.md``.
"""

from __future__ import annotations

from pathlib import Path

from alla_skill_lib import workspace as ws

PROBLEM_PREFIX = "Проблема скилла:"

EXECUTOR_RULES = f"""\
- Ты исполнитель сценария скилла: делай только то, что сказано в задании и в ответе
  скрипта, по порядку; ничего не добавляй, не пропускай и не обходи.
- Файлы скилла (SKILL.md, references/, scripts/, tests/) только читай.
- Своих скриптов не пиши: ни файлов Python/bash/node, ни `python -c`, ни heredoc, ни
  циклов вокруг команд скилла; из shell — только команды скилла из задания и ответов
  скрипта.
- Разбор пиши только через write_file и только в названные файлы; `.env`, `run.json`,
  `evidence/` и чужие файлы не читай и не меняй.
- Формат неясен, проверка отклоняет файл, верный по справочнику, или задание
  противоречит себе — не обходи: продолжай по сценарию и в конце ответа напиши
  «{PROBLEM_PREFIX} <где и что не так>»."""

ANALYSIS_FORMAT_REF = "analysis-format.md"
PROPOSAL_FORMAT_REF = "proposal-format.md"
SUMMARY_FORMAT_REF = "summary-format.md"
FEEDBACK_FORMAT_REF = "feedback.md"
PROBLEM_REPORT_REF = "problem-report.md"

MAX_PROJECT_FILES = 3


def reference_path(name: str) -> Path:
    """Абсолютный путь справочника (``ws.SKILL_DIR`` читается при вызове — его подменяют тесты)."""
    return ws.SKILL_DIR / "references" / name


def reference_line(name: str, what: str = "Формат с примерами и типичными ошибками") -> str:
    return f"{what}: {reference_path(name)}"
