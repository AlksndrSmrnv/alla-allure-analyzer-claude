"""Сводка привязана к данным задания; история обновляется независимо от неё."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from test_skill_flow import MARKDOWN_ANALYSIS, VALID_ANALYSIS, _next, _prepare, _run
from test_skill_report import APP, _run as report_run

from alla_skill_lib import report, workspace as ws
from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.history import load_history
from alla_skill_lib.sources import check_entry_analysis


def _ready(project: Path, capsys) -> tuple[Path, dict, str]:
    directory, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (directory / "analyses" / f"{order}.md").write_text(VALID_ANALYSIS, encoding="utf-8")
    (directory / "analyses" / f"{login}.md").write_text(MARKDOWN_ANALYSIS, encoding="utf-8")
    return directory, run, order


def _complete(directory: Path, capsys, summary: str = "Прежняя сводка.") -> None:
    assert _next(directory, capsys).startswith("STATUS: summary")
    (directory / "summary.md").write_text(summary, encoding="utf-8")
    assert _next(directory, capsys).startswith("STATUS: done")


def _state(directory: Path) -> dict:
    return json.loads((directory / "state.json").read_text(encoding="utf-8"))


def test_summary_hash_uses_the_same_data_block_as_task(
    project: Path, testops, capsys, monkeypatch
) -> None:
    directory, run, _ = _ready(project, capsys)
    assert _next(directory, capsys).startswith("STATUS: summary")
    analyses = {
        entry["file_id"]: check_entry_analysis(
            (directory / "analyses" / f"{entry['file_id']}.md").read_text(encoding="utf-8"),
            entry, Path(run["project_root"]), ws.RunPaths(directory))[0]
        for entry in run["clusters"]
    }
    data = report.build_summary_data(run, analyses, set())
    # Причина «окружение» у login опирается только на сообщение теста — пометка в сводке.
    assert "не принимает соединения. (причина не подтверждена логом)" in data
    task = (directory / "summary_task.md").read_text(encoding="utf-8")
    assert data in task
    assert _state(directory)["summary_data_hash"] == hashlib.sha256(data.encode()).hexdigest()
    for name in ("SUMMARY_RULES", "EXECUTOR_RULES", "UNTRUSTED_NOTE"):
        monkeypatch.setattr(report, name, f"Новые правила: {name}.")
    moved = directory.parent / "moved project" / directory.name
    monkeypatch.setattr(ws, "ENTRYPOINT", moved / "skill" / "alla_skill.py")
    relocated_run = {**run, "project_root": str(moved / "project")}
    assert report.build_summary_data(relocated_run, analyses, set()) == data
    rewritten = report.build_summary_task(relocated_run, analyses, set(), ws.RunPaths(moved))
    assert rewritten != task
    assert str(moved / "summary.md") in rewritten
    assert str(ws.ENTRYPOINT) in rewritten
    assert all(f"Новые правила: {name}." in rewritten
               for name in ("SUMMARY_RULES", "EXECUTOR_RULES", "UNTRUSTED_NOTE"))
    assert data in rewritten


def test_summary_data_tracks_compact_inputs_and_tail_categories(tmp_path: Path) -> None:
    # Смена compact/лимитов или формата данных намеренно обновляет хэш сводки.
    run = report_run([1] * 41)
    analyses = {entry["file_id"]: parse_analysis(APP) for entry in run["clusters"]}
    data = report.build_summary_data(run, analyses, set())
    assert "Ещё 1 проблема" in data
    assert data in report.build_summary_task(run, analyses, set(), ws.RunPaths(tmp_path))

    analyses["41"] = parse_analysis(APP.replace("Заказ не создаётся", "Заказ не отправляется"))
    assert report.build_summary_data(run, analyses, set()) == data  # в хвосте только категории
    analyses["41"] = parse_analysis(APP.replace("ПРИЧИНА: приложение", "ПРИЧИНА: окружение"))
    assert report.build_summary_data(run, analyses, set()) != data
    analyses["41"] = parse_analysis(APP)
    assert report.build_summary_data(run, analyses, {"01"}) != data


def test_changed_analysis_requests_a_new_summary(project: Path, testops, capsys) -> None:
    directory, _, order = _ready(project, capsys)
    _complete(directory, capsys)
    previous_hash = _state(directory)["summary_data_hash"]
    (directory / "analyses" / f"{order}.md").write_text(
        VALID_ANALYSIS.replace("OrderService.create падает на пустом customer.",
                               "OrderService.create не проверяет customer."),
        encoding="utf-8",
    )
    assert _next(directory, capsys).startswith("STATUS: summary")
    assert not (directory / "summary.md").exists()
    assert _state(directory)["summary_data_hash"] != previous_hash
    assert "не проверяет customer" in (directory / "summary_task.md").read_text(encoding="utf-8")
    (directory / "summary.md").write_text("Новая сводка.", encoding="utf-8")
    assert _next(directory, capsys).startswith("STATUS: done")
    latest = next(row for row in load_history(directory.parent) if row["file_id"] == order)
    assert latest["cause"] == "OrderService.create не проверяет customer."


def test_whitespace_changes_preserve_summary_and_history(project: Path, testops, capsys) -> None:
    directory, _, order = _ready(project, capsys)
    _complete(directory, capsys)
    previous_hash = _state(directory)["summary_data_hash"]
    history_before = (directory.parent / "history.jsonl").read_bytes()
    (directory / "analyses" / f"{order}.md").write_text(
        VALID_ANALYSIS.replace("OrderService.create падает на пустом customer.",
                               "OrderService.create   падает   на пустом customer."),
        encoding="utf-8",
    )
    assert _next(directory, capsys).startswith("STATUS: done")
    assert _state(directory)["summary_data_hash"] == previous_hash
    assert (directory / "summary.md").read_text(encoding="utf-8") == "Прежняя сводка."
    assert (directory.parent / "history.jsonl").read_bytes() == history_before


@pytest.mark.parametrize("completed", [False, True])
def test_legacy_summary_binds_without_rewriting(
    project: Path, testops, capsys, completed: bool
) -> None:
    directory, _, _ = _ready(project, capsys)
    _complete(directory, capsys)
    state = _state(directory)
    state.pop("summary_data_hash", None)
    state["history_written"] = "legacy marker"
    ws.write_json(directory / "state.json", state)
    if not completed:
        (directory / "report.md").unlink()
    history_before = (directory.parent / "history.jsonl").read_bytes()

    assert _next(directory, capsys).startswith("STATUS: done")
    assert _state(directory)["summary_data_hash"]
    assert _state(directory)["history_written"] == "legacy marker"
    assert (directory / "summary.md").read_text(encoding="utf-8") == "Прежняя сводка."
    assert (directory.parent / "history.jsonl").read_bytes() == history_before


def test_skip_invalidates_legacy_summary_without_resetting_history_flag(
    project: Path, testops, capsys
) -> None:
    directory, _, order = _ready(project, capsys)
    _complete(directory, capsys)
    state = _state(directory)
    state.pop("summary_data_hash", None)
    state["history_written"] = True
    ws.write_json(directory / "state.json", state)
    history_before = (directory.parent / "history.jsonl").read_text(encoding="utf-8").splitlines()

    code, out = _run(["skip", order, "--run", str(directory)], capsys)
    assert code == 0 and out.startswith("STATUS: saved")
    assert not (directory / "summary.md").exists()
    assert _state(directory)["history_written"] is True
    assert _next(directory, capsys).startswith("STATUS: summary")
    (directory / "summary.md").write_text("Проблема пропущена.", encoding="utf-8")
    assert _next(directory, capsys).startswith("STATUS: done")
    history_after = (directory.parent / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history_after) == len(history_before) + 1
    latest = next(row for row in load_history(directory.parent) if row["file_id"] == order)
    assert latest["category"] == "неизвестно"


@pytest.mark.parametrize("change", ["edit", "skip"])
def test_analysis_changed_after_summary_task_rejects_just_written_old_summary(
    project: Path, testops, capsys, change: str
) -> None:
    directory, _, order = _ready(project, capsys)
    assert _next(directory, capsys).startswith("STATUS: summary")
    if change == "skip":
        assert _run(["skip", order, "--run", str(directory)], capsys)[0] == 0
    else:
        (directory / "analyses" / f"{order}.md").write_text(
            VALID_ANALYSIS.replace("падает на пустом customer", "не проверяет customer"),
            encoding="utf-8",
        )
    (directory / "summary.md").write_text("Сводка по прежнему заданию.", encoding="utf-8")
    assert _next(directory, capsys).startswith("STATUS: summary")
    assert not (directory / "summary.md").exists()


@pytest.mark.parametrize("new_ref", ["нет", "known_2"])
def test_kb_reference_changes_update_history_without_refreshing_summary(
    project: Path, testops, capsys, new_ref: str
) -> None:
    directory, run, order = _ready(project, capsys)
    entry = next(entry for entry in run["clusters"] if entry["file_id"] == order)
    entry["kb"] = [{"id": "known_1"}, {"id": "known_2"}]
    ws.write_json(directory / "run.json", run)
    analysis = directory / "analyses" / f"{order}.md"
    analysis.write_text(VALID_ANALYSIS + "БАЗА ЗНАНИЙ: known_1\n", encoding="utf-8")
    _complete(directory, capsys)
    state = _state(directory)
    previous_hash = state["summary_data_hash"]
    state["history_written"] = True
    ws.write_json(directory / "state.json", state)
    history_before = (directory.parent / "history.jsonl").read_text(encoding="utf-8").splitlines()

    analysis.write_text(VALID_ANALYSIS + f"БАЗА ЗНАНИЙ: {new_ref}\n", encoding="utf-8")
    assert _next(directory, capsys).startswith("STATUS: done")
    assert _state(directory)["summary_data_hash"] == previous_hash
    assert _state(directory)["history_written"] is True
    history_after = (directory.parent / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history_after) == len(history_before) + 1
    latest = next(row for row in load_history(directory.parent) if row["file_id"] == order)
    assert latest["kb_entry"] == (None if new_ref == "нет" else new_ref)
    assert _next(directory, capsys).startswith("STATUS: done")
    assert (directory.parent / "history.jsonl").read_text(encoding="utf-8").splitlines() == history_after


def test_unchanged_history_ignores_a_false_legacy_flag(project: Path, testops, capsys) -> None:
    directory, _, _ = _ready(project, capsys)
    _complete(directory, capsys)
    state = _state(directory)
    assert "history_written" not in state
    state["history_written"] = False
    ws.write_json(directory / "state.json", state)
    before = (directory.parent / "history.jsonl").read_bytes()
    assert _next(directory, capsys).startswith("STATUS: done")
    assert (directory.parent / "history.jsonl").read_bytes() == before
    assert _state(directory)["history_written"] is False
