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
- Ты исполнитель сценария скилла: делай только то, что написано в задании и в ответе
  скрипта, по порядку. Ничего не добавляй, не пропускай и не обходи.
- Файлы скилла (SKILL.md, references/, scripts/, tests/) только читай: не правь, не
  удаляй и не копируй в «исправленном» виде.
- Своих скриптов не пиши: ни файлов Python/bash/node, ни `python -c`, ни heredoc,
  ни циклов вокруг команд скилла. Из shell — только команды скилла, которые названы в
  задании или в ответе скрипта.
- Файлы разбора пиши только инструментом write_file и только те, что названы в
  задании. `.env`, `run.json`, `evidence/` и чужие файлы не читай и не меняй.
- Если формат неясен, проверка отклоняет файл, который соответствует справочнику формата,
  или задание противоречит само себе — не обходи это. Продолжай по сценарию и напиши
  в конце ответа строку «{PROBLEM_PREFIX} <где и что не так>»."""

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
