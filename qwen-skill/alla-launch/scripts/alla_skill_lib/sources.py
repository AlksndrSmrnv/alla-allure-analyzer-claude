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

Registry = dict[str, dict[str, Any]]


def registry(
    sources: tuple[PromptSource, ...],
    message_test: FailedTestSummary | None,
    log_test: FailedTestSummary | None,
) -> Registry:
    """id → вид, тест, вложение (имя и id), строки и текст куска данных."""
    result: Registry = {}
    for source in sources:
        test = log_test if source.kind == "log" else message_test
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
    return data if isinstance(data, dict) else None


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
