"""Задание субагенту на пакет кластеров: ``batches/N.md``.

Когда кластеров много, основной агент раздаёт их пакетами субагентам Qwen Code
(инструмент ``agent``). Субагент стартует с чистым контекстом и видит только
этот файл, поэтому он самодостаточен: правила, список «кластер → задание →
файл разбора» и команда проверки ``verify``. Правила анализа и формат ответа
лежат в ``clusters/NN.md`` — здесь только то, что относится к работе субагента.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.agent_rules import (
    ANALYSIS_FORMAT_REF,
    EXECUTOR_RULES,
    PROBLEM_PREFIX,
    reference_line,
)
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE

MAX_VERIFY_ROUNDS = 3
# Свой субагент Qwen для пакетов: у встроенного general-purpose системная подсказка велит
# сначала осматривать проект, и субагенты смотрели папки через ls/find вопреки правилам.
BATCH_AGENT = "alla-batch"

logger = logging.getLogger(__name__)


def batch_agent_source() -> Path:
    """Описание субагента в папке скилла (``ws.SKILL_DIR`` читается при вызове — его подменяют тесты)."""
    return ws.SKILL_DIR / "agents" / f"{BATCH_AGENT}.md"


def install_batch_agent(project_root: Path) -> Path | None:
    """Положить описание субагента в ``<проект>/.qwen/agents/``, если его нет или оно другое.

    Qwen Code читает агентов при старте сеанса: только что установленного агента увидит
    следующий сеанс, а в текущем ``agent`` ответит, что такого типа нет (на это есть запасной
    путь в ответе ``next``). Возвращает путь, если файл записан.
    """
    source = batch_agent_source()
    if not source.is_file():
        return None
    target = project_root / ".qwen" / "agents" / source.name
    text = source.read_text(encoding="utf-8")
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        ws.write_text(target, text)
    except OSError as error:
        logger.warning("Не удалось установить субагента %s: %s", target, error)
        return None
    return target


def verify_command(paths: ws.RunPaths, file_ids: list[str]) -> str:
    return ws.skill_command("verify", *file_ids, "--run", str(paths.root))


def render_batch_task(
    paths: ws.RunPaths,
    number: int,
    entries: list[dict[str, Any]],
    launch_id: int,
) -> str:
    """Текст ``batches/N.md`` для пакета ``entries`` (записи ``run.json``)."""
    ids = [entry["file_id"] for entry in entries]
    lines = [
        f"# Пакет {number}: кластеры {', '.join(ids)} · прогон #{launch_id}",
        "",
        "Ты — субагент: разбираешь только кластеры этого пакета, остальные разбирают другие. "
        "Основной агент ждёт твоего ответа.",
        "",
        UNTRUSTED_NOTE,
        "",
        "## Что делать",
        "Для каждого кластера из списка по порядку:",
        "1. Прочитай его задание (read_file): правила, данные и формат ответа — в нём. "
        "Строку «Затем выполни: …» в его начале пропусти.",
        "2. Запиши разбор в указанный файл инструментом write_file.",
        reference_line(ANALYSIS_FORMAT_REF, "   Формат с примерами и типичными ошибками"),
        "",
        "Кластеры пакета:",
    ]
    for entry in entries:
        label = " ".join(str(entry["label"]).split())
        if len(label) > 100:
            label = label[:99] + "…"
        lines += [
            f"- {entry['file_id']} · {label} ({entry['member_count']} тест.)",
            f"  задание: {paths.cluster_task(entry['file_id'])}",
            f"  разбор:  {paths.analysis(entry['file_id'])}",
        ]
    lines += [
        "",
        "## Проверка",
        "Когда записал все разборы, выполни одну команду (shell):",
        "",
        f"    {verify_command(paths, ids)}",
        "",
        "`STATUS: ok` — всё принято. `STATUS: fix` — исправь названные файлы (write_file) и "
        f"повтори проверку; не больше {MAX_VERIFY_ROUNDS} раз на кластер. Если разбор так и не "
        "прошёл — оставь как есть и назови номер в ответе.",
        "",
        "## Нельзя",
        "- Команды скилла, кроме проверки выше: next, skip, apply, revert, remember, reject, "
        "prepare — они не для тебя.",
        "- Запускать других субагентов, тесты, сборку, git, curl; читать сырые логи.",
        "",
        "## Правила исполнителя",
        EXECUTOR_RULES,
        "",
        "## Ответ основному агенту",
        "Одной строкой, без пересказа разборов: «Готово: 03, 04.» или "
        "«Готово: 03, 04. Не прошли проверку: 05 — причина.» Если заметил неполадку "
        f"скилла — добавь в конец строки «{PROBLEM_PREFIX} <где и что не так>».",
    ]
    return "\n".join(lines) + "\n"
