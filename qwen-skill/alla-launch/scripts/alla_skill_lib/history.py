"""Локальная история разборов: ``alla-reports/history.jsonl``.

После каждого завершённого разбора сюда дописывается по строке на кластер.
Следующий ``prepare`` находит прошлые разборы той же ошибки (по сигнатуре,
грубому ключу или записи базы знаний) и показывает повторы в задании и
отчёте. История локальная и в git не попадает (``alla-reports/.gitignore``).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alla_skill_lib.kb import normalize_fp

if TYPE_CHECKING:
    from alla_skill_lib.analysis_format import ClusterAnalysis

HISTORY_FILE = "history.jsonl"
MAX_CAUSE_CHARS = 200

_FRAME_RE = re.compile(r"^\s*(?:at\s|File\s\")")


def loose_key(message: str, trace: str) -> str | None:
    """Грубый ключ ошибки: первая строка сообщения (или исключения) без чисел и ID."""
    line = next((item.strip() for item in message.splitlines() if item.strip()), "")
    if not line:
        line = next(
            (item.strip() for item in trace.splitlines() if item.strip() and not _FRAME_RE.match(item)),
            "",
        )
    if not line:
        return None
    return hashlib.sha1(normalize_fp(line).encode("utf-8")).hexdigest()[:16]


def load_history(reports_dir: Path) -> list[dict[str, Any]]:
    """Записи истории; битые строки пропускаются."""
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
    return records


def recurrence(
    history: list[dict[str, Any]],
    *,
    launch_id: int,
    signature: str | None,
    loose: str | None,
    kb_ids: set[str],
) -> dict[str, Any] | None:
    """Сводка прошлых разборов той же ошибки в других прогонах (None — не встречалась)."""
    matches = [
        record for record in history
        if record.get("launch_id") != launch_id
        and (
            (signature and record.get("signature") == signature)
            or (loose and record.get("loose_key") == loose)
            or (record.get("kb_entry") and record.get("kb_entry") in kb_ids)
        )
    ]
    if not matches:
        return None
    matches.sort(key=lambda record: str(record.get("date", "")))
    last = matches[-1]
    return {
        "launches": len({record["launch_id"] for record in matches}),
        "first_date": matches[0].get("date"),
        "last": {
            "date": last.get("date"),
            "launch_id": last.get("launch_id"),
            "category": last.get("category"),
            "cause": last.get("cause"),
        },
    }


def run_records(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    run_name: str,
) -> list[dict[str, Any]]:
    """Записи истории по завершённому разбору (кластеры без данных пропускаются)."""
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
            "loose_key": entry.get("loose_key"),
            "label": str(entry["label"])[:MAX_CAUSE_CHARS],
            "category": analysis.category if trusted and analysis else None,
            "cause": " ".join(analysis.cause_reason.split())[:MAX_CAUSE_CHARS]
            if trusted and analysis else None,
            "members": entry["member_count"],
            "kb_entry": analysis.kb_ref if trusted and analysis else None,
        })
    return records


def append_run(reports_dir: Path, records: list[dict[str, Any]]) -> None:
    """Дописать записи одного разбора одним вызовом записи."""
    if not records:
        return
    lines = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    with (reports_dir / HISTORY_FILE).open("a", encoding="utf-8") as stream:
        stream.write(lines)


def render_recurrence(info: dict[str, Any], *, has_exact_kb: bool) -> list[str]:
    """Раздел «Прошлые разборы» для задания кластера."""
    launches = int(info["launches"])
    where = "другом прогоне" if launches % 10 == 1 and launches % 100 != 11 else "других прогонах"
    lines = [
        "--- Прошлые разборы этой ошибки ---",
        f"Встречалась в {launches} {where}, впервые {format_date(info['first_date'])}.",
    ]
    if has_exact_kb:
        lines.append("Подтверждённая причина — в базе знаний проекта выше.")
        return lines
    last = info["last"]
    if last.get("category"):
        lines.append(
            f"Последний вывод (прогон #{last['launch_id']}, {format_date(last['date'])}; "
            f"вывод прошлого разбора, пользователем не подтверждён): "
            f"{last['category']} — {last.get('cause') or ''}".rstrip(" —")
        )
    return lines


def format_date(value: Any) -> str:
    """``2026-09-28`` → ``28.09.2026``."""
    text = str(value or "")
    parts = text[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else text
