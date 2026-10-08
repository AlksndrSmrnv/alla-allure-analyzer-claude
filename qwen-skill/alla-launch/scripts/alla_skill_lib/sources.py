"""Реестр источников кластера: ``evidence/NN.sources.json``.

Каждый кусок данных задания идёт под своим id: ``S1`` — сообщение об ошибке,
``S2`` — стек-трейс, дальше фрагменты лога. Реестр хранит ровно тот текст, что
видела модель, и откуда он: тест, вложение, строки. По нему проверяются цитаты
раздела «НАБЛЮДЕНИЯ» и строятся ссылки на источник в отчёте.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alla_core.models.testops import FailedTestSummary
from alla_core.services.prompt_builder_service import PromptSource

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import (
    ClusterAnalysis,
    parse_analysis,
    validate_analysis,
)

Registry = dict[str, dict[str, Any]]


def registry(
    sources: tuple[PromptSource, ...],
    tests_by_id: dict[int, FailedTestSummary],
) -> Registry:
    """id → вид, тест, вложение (имя и id), строки и текст куска данных."""
    result: Registry = {}
    for source in sources:
        test = tests_by_id.get(source.test_result_id) if source.test_result_id else None
        attachment_id = None
        if source.attachment and test is not None:
            attachment_id = next(
                (ref.id for ref in test.log_attachments if ref.name == source.attachment), None)
        result[source.id] = {
            "kind": source.kind,
            "test_result_id": test.test_result_id if test else None,
            "test_name": source.test_name,
            "section": source.section,
            "attachment": source.attachment,
            "attachment_id": attachment_id,
            "lines": source.lines,
            "text": source.text,
        }
    return result


def write_registry(path: Path, records: Registry) -> None:
    ws.write_json(path, records)


def load_registry(path: Path) -> Registry | None:
    """Реестр кластера; ``None`` — файла нет или он повреждён."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(record, dict) and isinstance(record.get("text"), str)
        for key, record in data.items()
    ):
        return None
    return data


def describe(record: dict[str, Any]) -> str:
    """Источник для человека: «лог app.log, строки 120–134, тест createOrder»."""
    kind = record.get("kind")
    if kind == "message":
        what = "сообщение об ошибке"
    elif kind == "trace":
        what = "стек-трейс"
    else:
        what = " ".join(str(part) for part in (record.get("section") or "лог",
                                               record.get("attachment")) if part)
        lines = str(record.get("lines") or "").split(" · ", 1)[0]
        if lines:
            what += f", {lines}"
    if record.get("test_name"):
        what += f", тест {record['test_name']}"
    return what


def observed(entry: dict[str, Any]) -> bool:
    """Разбор кластера — с наблюдениями по реестру источников (у ``auto`` разбор пишет код)."""
    return not entry.get("auto")


def example_blocks(entry: dict[str, Any]) -> int:
    """Сколько примеров было в задании отдельными блоками (у ``auto`` задания нет — один)."""
    return int(entry.get("example_blocks") or 1)


def check_entry_analysis(
    text: str,
    entry: dict[str, Any],
    project_root: Path,
    paths: ws.RunPaths,
) -> tuple[ClusterAnalysis, list[str]]:
    """Разобрать и проверить разбор кластера по правилам его формата.

    Общая проверка для ``next``, ``verify`` и ``remember --from-analysis``. К
    разбору с наблюдениями (у всех, кроме ``auto``) прикреплён реестр источников —
    для подписи цитат в отчёте.
    """
    analysis = parse_analysis(text)
    offered = frozenset(match["id"] for match in entry.get("kb", []))
    with_sources = observed(entry)
    sources = load_registry(paths.sources(entry["file_id"])) if with_sources else None
    analysis.sources = sources
    errors = validate_analysis(analysis, project_root, offered, observed=with_sources,
                               sources=sources, examples=example_blocks(entry))
    return analysis, errors
