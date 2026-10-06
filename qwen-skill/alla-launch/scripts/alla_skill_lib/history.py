"""Локальная история разборов: ``alla-reports/history.jsonl``.

После завершения разбора сюда дописываются новые или изменённые строки кластеров;
при чтении берётся последняя версия каждой пары (разбор, кластер).
Следующий ``prepare`` находит прошлые разборы той же ошибки (по точной
сигнатуре или подтверждённой записи базы знаний) и показывает число
повторов в задании и отчёте. Прошлые выводы модели в задание не подаются:
непроверенная догадка иначе стала бы якорем для следующего разбора.
История локальная и в git не попадает (``alla-reports/.gitignore``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alla_skill_lib.analysis_format import ClusterAnalysis

HISTORY_FILE = "history.jsonl"
MAX_CAUSE_CHARS = 200

def load_history(reports_dir: Path) -> list[dict[str, Any]]:
    """Последняя версия каждого кластера; битые строки пропускаются, legacy остаётся."""
    path = reports_dir / HISTORY_FILE
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "launch_id" in record:
            records.append(record)
    latest: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in reversed(records):
        key = _record_key(record)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        latest.append(record)
    return latest[::-1]


def _record_key(record: dict[str, Any]) -> tuple[str, str] | None:
    run, file_id = record.get("run"), record.get("file_id")
    if isinstance(run, str) and run and isinstance(file_id, str) and file_id:
        return run, file_id
    return None


def recurrence(
    history: list[dict[str, Any]],
    *,
    launch_id: int,
    signature: str | None,
    kb_ids: set[str],
    module: str = "",
) -> dict[str, Any] | None:
    """Сводка прошлых разборов той же ошибки в других прогонах (None — не встречалась).

    Учитываются только разборы того же модуля: одинаковый текст ошибки в разных
    модулях — разные проблемы. Записи без поля ``module`` относятся к корню.
    """
    matches = [
        record for record in history
        if record.get("launch_id") != launch_id
        and record.get("module", "") == module
        and (
            (signature and record.get("signature") == signature)
            or (record.get("kb_entry") and record.get("kb_entry") in kb_ids)
        )
    ]
    if not matches:
        return None
    matches.sort(key=lambda record: str(record.get("date", "")))
    return {
        "launches": len({record["launch_id"] for record in matches}),
        "first_date": matches[0].get("date"),
        "last_date": matches[-1].get("date"),
    }


def run_records(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    run_name: str,
    known_refs: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Записи истории по завершённому разбору (кластеры без данных пропускаются).

    ``known_refs`` — подтверждённые ссылки на записи базы знаний (file_id → id, см.
    ``known_issues``): отвергнутая после разбора запись не должна считать повторы. Без
    него — ссылка из разбора как есть.
    """
    records: list[dict[str, Any]] = []
    for entry in run["clusters"]:
        if entry.get("auto") or not entry.get("signature"):
            continue
        analysis = analyses.get(entry["file_id"])
        trusted = analysis is not None and entry["file_id"] not in flagged
        records.append({
            "date": str(run.get("created_at", ""))[:10],
            "launch_id": run["launch_id"],
            "launch_name": run.get("launch_name"),
            "run": run_name,
            "file_id": entry["file_id"],
            "signature": entry["signature"],
            "module": entry.get("module", ""),
            "label": str(entry["label"])[:MAX_CAUSE_CHARS],
            "category": analysis.category if trusted and analysis else None,
            "cause": " ".join(analysis.cause_reason.split())[:MAX_CAUSE_CHARS]
            if trusted and analysis else None,
            "members": entry["member_count"],
            "kb_entry": (
                known_refs.get(entry["file_id"]) if known_refs is not None
                else analysis.kb_ref if trusted and analysis else None
            ),
        })
    return records


def append_changed_run(reports_dir: Path, records: list[dict[str, Any]]) -> None:
    """Дописать только отсутствующие или изменённые строки завершённого разбора."""
    latest = {
        key: record for record in load_history(reports_dir)
        if (key := _record_key(record)) is not None
    }
    changed: list[dict[str, Any]] = []
    for record in records:
        key = _record_key(record)
        if key is None or latest.get(key) != record:
            changed.append(record)
            if key is not None:
                latest[key] = record
    append_run(reports_dir, changed)


def append_run(reports_dir: Path, records: list[dict[str, Any]]) -> None:
    """Дописать записи одного разбора одним вызовом записи."""
    if not records:
        return
    path = reports_dir / HISTORY_FILE
    lines = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    # Оборванная прошлая запись без перевода строки иначе склеилась бы со следующей.
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                lines = "\n" + lines
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(lines)


def render_recurrence(info: dict[str, Any], *, has_exact_kb: bool) -> list[str]:
    """Раздел «Прошлые разборы» для задания кластера."""
    launches = int(info["launches"])
    where = "другом прогоне" if launches % 10 == 1 and launches % 100 != 11 else "других прогонах"
    lines = [
        "--- Прошлые разборы этой ошибки ---",
        f"Встречалась в {launches} {where}, впервые разобрана {format_date(info['first_date'])}.",
    ]
    if has_exact_kb:
        lines.append("Подтверждённая причина — в базе знаний проекта выше.")
    return lines


def format_date(value: Any) -> str:
    """``2026-09-28`` → ``28.09.2026``."""
    text = str(value or "")
    parts = text[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else text
