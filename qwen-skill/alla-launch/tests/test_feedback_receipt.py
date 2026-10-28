"""Только действительно сохранённый ввод инженера становится принятым диагнозом."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import FakeTestOps
from test_skill_flow import VALID_ANALYSIS, _prepare, _run

from alla_skill_lib import feedback, workspace as ws
from alla_skill_lib.kb import ProjectKB

CORRECTION = (
    "ПРИЧИНА: окружение — На стенде исчерпан пул соединений.\n"
    "КАК ИСПРАВИТЬ:\n1. Восстановить пул соединений на стенде.\n"
)


@pytest.fixture
def case(project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str]):
    run_dir, run, _ = _prepare(project, capsys)
    paths = ws.RunPaths(run_dir)
    entry = next(item for item in run["clusters"] if not item["auto"])
    paths.analysis(entry["file_id"]).write_text(VALID_ANALYSIS + "\nСОГЛАСОВАННОСТЬ: одна причина\n")
    paths.feedback(entry["file_id"]).write_bytes(CORRECTION.encode("utf-8"))
    return paths, run, entry


def save(case: Any, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    paths, run, entry = case
    code, output = _run(["remember", entry["file_id"], "--run", str(paths.root), *extra], capsys)
    assert code == 0 and output.startswith("STATUS: saved"), output
    record_id = ws.read_json(paths.state_json)["accepted_feedback"][entry["file_id"]]["record_id"]
    assert feedback.accepted_feedback(paths, run, entry)
    return record_id


def test_receipt_is_created_after_save_and_preserves_unrelated_state(case: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    paths, run, entry = case
    ws.write_json(paths.state_json, {"attempts": {"01": 2}, "summary_data_hash": "kept",
                                    "refusals": {entry["file_id"]: 2, "99": 1}})
    assert feedback.accepted_feedback(paths, run, entry) is None
    assert feedback.feedback_pending(paths, entry)
    record_id = save(case, capsys)
    state = ws.read_json(paths.state_json)
    assert state["attempts"] == {"01": 2} and state["summary_data_hash"] == "kept"
    assert state["refusals"] == {"99": 1}
    before = paths.state_json.read_bytes()
    accepted = feedback.accepted_feedback(paths, run, entry, state=state)
    assert accepted and accepted["category"] == "окружение"
    assert accepted["cause"] == "На стенде исчерпан пул соединений."
    assert accepted["fix"] == ["Восстановить пул соединений на стенде."]
    assert accepted["text"] == CORRECTION and accepted["record_id"] == record_id
    assert paths.state_json.read_bytes() == before


def test_failed_remember_does_not_create_or_refresh_receipt(case: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    paths, run, entry = case
    path = paths.feedback(entry["file_id"])
    path.write_text(CORRECTION.replace("окружение", "неизвестно"))
    code, output = _run(["remember", entry["file_id"], "--run", str(paths.root)], capsys)
    assert code == 0 and output.startswith("STATUS: fix"), output
    assert "accepted_feedback" not in ws.read_json(paths.state_json)
    path.write_text(CORRECTION)
    record_id = save(case, capsys)
    mark = ws.read_json(paths.state_json)["accepted_feedback"][entry["file_id"]]
    path.write_text(CORRECTION.replace("окружение", "неизвестно"))
    code, output = _run(["remember", entry["file_id"], "--entry", record_id,
                         "--run", str(paths.root)], capsys)
    assert code == 0 and output.startswith("STATUS: fix"), output
    assert ws.read_json(paths.state_json)["accepted_feedback"][entry["file_id"]] == mark
    assert feedback.accepted_feedback(paths, run, entry) is None


@pytest.mark.parametrize("edit", ["\n", " ", "\r\n"])
def test_even_whitespace_edits_need_a_new_successful_remember(case: Any,
        capsys: pytest.CaptureFixture[str], edit: str) -> None:
    paths, run, entry = case
    record_id = save(case, capsys)
    path = paths.feedback(entry["file_id"])
    path.write_bytes((CORRECTION + edit).encode("utf-8"))
    assert feedback.accepted_feedback(paths, run, entry) is None
    assert feedback.feedback_pending(paths, entry)
    assert save(case, capsys, "--entry", record_id) == record_id


@pytest.mark.parametrize("field,value", [
    ("description", "Другая причина"), ("resolution_steps", ["Другой рецепт"]),
    ("category", "service"), ("error_example", "другой признак"),
])
def test_changed_record_semantics_invalidates_receipt(case: Any,
        capsys: pytest.CaptureFixture[str], field: str, value: Any) -> None:
    paths, run, entry = case
    record_id = save(case, capsys)
    kb = ProjectKB(Path(entry["kb_dir"]))
    record = kb.get(record_id)
    assert record is not None
    setattr(record, field, value)
    kb.save(record)
    assert feedback.accepted_feedback(paths, run, entry) is None


@pytest.mark.parametrize("change", ["delete", "reject", "reassign"])
def test_lost_record_binding_invalidates_receipt(case: Any,
        capsys: pytest.CaptureFixture[str], change: str) -> None:
    paths, run, entry = case
    record_id = save(case, capsys)
    kb = ProjectKB(Path(entry["kb_dir"]))
    if change == "delete":
        kb.path_for(record_id).unlink()
    elif change == "reject":
        code, output = _run(["reject", entry["file_id"], record_id,
                             "--run", str(paths.root)], capsys)
        assert code == 0, output
        assert entry["file_id"] not in ws.read_json(paths.state_json).get("accepted_feedback", {})
    else:
        record = kb.get(record_id)
        assert record is not None
        record.confirmed_signatures = []
        record.confirmed_tests = []
        kb.save(record)
    assert feedback.accepted_feedback(paths, run, entry) is None


def test_unrelated_record_metadata_does_not_change_accepted_diagnosis(case: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    paths, run, entry = case
    record_id = save(case, capsys)
    kb = ProjectKB(Path(entry["kb_dir"]))
    record = kb.get(record_id)
    assert record is not None
    record.created["other"] = "metadata"
    record.extra["tracker"] = "T-1"
    record.confirm("unrelated-signature", ["other-test"], ["other-log"])
    kb.save(record)
    assert feedback.accepted_feedback(paths, run, entry)


@pytest.mark.parametrize("source", ["analysis", "extension"])
def test_successful_other_source_ignores_only_unchanged_old_feedback(case: Any,
        capsys: pytest.CaptureFixture[str], source: str) -> None:
    paths, run, entry = case
    record_id = save(case, capsys)
    if source == "extension":
        ws.write_json(paths.extension_dir("sut-code") / f"on_request-{entry['file_id']}.finding.json", {
            "problem": int(entry["file_id"]), "format_ok": True, "category": "приложение",
            "title": "Не проверен покупатель", "cause": "Приложение не проверяет покупателя.",
            "fix": ["Проверить покупателя до создания заказа."], "details": [],
        })
    flag = ["--from-analysis"] if source == "analysis" else ["--from-extension", "sut-code"]
    code, output = _run(["remember", entry["file_id"], "--entry", record_id,
                         "--run", str(paths.root), *flag], capsys)
    assert code == 0 and output.startswith("STATUS: saved"), output
    assert feedback.accepted_feedback(paths, run, entry) is None
    assert not feedback.feedback_pending(paths, entry)
    assert paths.feedback(entry["file_id"]).read_text() == CORRECTION
    with paths.feedback(entry["file_id"]).open("a") as handle:
        handle.write("\n")
    assert feedback.feedback_pending(paths, entry)


def test_forged_matching_record_without_success_marker_is_not_accepted(case: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    paths, run, entry = case
    save(case, capsys)
    state = ws.read_json(paths.state_json)
    state.pop("accepted_feedback")
    ws.write_json(paths.state_json, state)
    assert feedback.accepted_feedback(paths, run, entry) is None

