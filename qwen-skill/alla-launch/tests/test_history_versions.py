"""Append-only история: читается последняя версия, пишутся только изменённые строки."""

from __future__ import annotations

import json
from pathlib import Path

from skill_fixtures import without_libmagic  # noqa: F401

from alla_skill_lib import history


def _row(run: str = "777-first", file_id: str = "01", **updates) -> dict:
    return {
        "run": run, "file_id": file_id, "launch_id": 777, "date": "2026-09-20",
        "signature": "v5:issue", "module": "", "category": "приложение",
        "cause": "Прежняя причина", "kb_entry": "known_1", **updates,
    }


def test_latest_physical_version_replaces_old_record_but_keeps_legacy(tmp_path: Path) -> None:
    legacy = {"launch_id": 12, "date": "2026-09-01", "signature": "v5:legacy"}
    before = _row(date="2026-09-25")
    latest = _row(date="2026-09-20", cause="Уточнённая причина", kb_entry=None)
    other = _row(file_id="02")
    history.append_run(tmp_path, [legacy, before, other, latest, latest])

    assert history.load_history(tmp_path) == [legacy, other, latest]
    assert history.recurrence(history.load_history(tmp_path), launch_id=999,
                              signature=None, kb_ids={"known_1"})["launches"] == 1
    only_updated = [row for row in history.load_history(tmp_path) if row.get("file_id") == "01"]
    assert history.recurrence(only_updated, launch_id=999,
                              signature=None, kb_ids={"known_1"}) is None


def test_append_changed_run_writes_only_missing_or_different_records(tmp_path: Path) -> None:
    before, unrelated = _row(), _row(run="778-other", launch_id=778)
    history.append_run(tmp_path, [before, unrelated])
    path = tmp_path / "history.jsonl"
    original = path.read_bytes()
    latest, added = _row(cause="Уточнённая причина", kb_entry=None), _row(file_id="02")

    history.append_changed_run(tmp_path, [latest, unrelated, added])
    content = path.read_bytes()
    assert content.startswith(original)
    assert [json.loads(line) for line in content.splitlines()] == [before, unrelated, latest, added]
    assert history.load_history(tmp_path) == [unrelated, latest, added]
    history.append_changed_run(tmp_path, [latest, unrelated, added])
    assert path.read_bytes() == content


def test_duplicate_retry_and_broken_tail_do_not_add_an_unchanged_version(tmp_path: Path) -> None:
    latest = _row(kb_entry=None)
    history.append_run(tmp_path, [_row(), latest, latest])
    path = tmp_path / "history.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"launch_id": 777, "cause":')
    before = path.read_bytes()
    history.append_changed_run(tmp_path, [latest])
    assert path.read_bytes() == before
    assert history.load_history(tmp_path) == [latest]

    changed = _row(cause="Изменено после оборванной записи", kb_entry=None)
    history.append_changed_run(tmp_path, [changed])
    assert path.read_bytes().startswith(before + b"\n")
    assert history.load_history(tmp_path) == [changed]


def test_latest_versions_preserve_unique_launch_counts_and_original_dates(tmp_path: Path) -> None:
    first = _row()
    corrected = _row(cause="Уточнено", kb_entry=None)
    second_run_same_launch = _row(run="777-second", date="2026-09-21")
    another_launch = _row(run="778-first", launch_id=778, date="2026-09-22")
    history.append_run(tmp_path, [first, second_run_same_launch, another_launch, corrected])

    rows = history.load_history(tmp_path)
    assert history.recurrence(rows, launch_id=999, signature="v5:issue", kb_ids=set()) == {
        "launches": 2, "first_date": "2026-09-20", "last_date": "2026-09-22",
    }
    assert len(rows) == 3
