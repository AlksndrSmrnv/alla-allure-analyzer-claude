"""Текущее состояние одинаково для отчёта и публичного снимка расширения."""
from __future__ import annotations

from pathlib import Path

import pytest

from skill_fixtures import project_fixture, testops_fixture  # noqa: F401
from skill_fake_testops import FakeTestOps, default_launch
from test_skill_flow import VALID_ANALYSIS, _prepare, _run
from test_skill_kb_auto import _remember_both
from test_skill_related import _next, _step_split_run

from alla_skill_lib import extension_data, workspace as ws
from alla_skill_lib.kb import KBRecord, ProjectKB
from alla_skill_lib.problem_state import current_state
from alla_skill_lib.report import render_report


@pytest.mark.parametrize("mode", ["analysis", "feedback", "remember", "confirmed", "known", "related"])
def test_report_and_snapshot_share_diagnosis_and_groups(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str,
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    paths = ws.RunPaths(run_dir)
    run = ws.read_run(paths)
    first = ids[0]
    if mode in ("feedback", "remember"):
        paths.feedback(first).write_text(
            "ПРИЧИНА: окружение — На стенде исчерпан пул соединений.\n"
            "КАК ИСПРАВИТЬ:\n1. Восстановить пул соединений на стенде.\n")
        if mode == "remember":
            code, output = _run(["remember", first, "--run", str(run_dir)], capsys)
            assert code == 0, output
    elif mode == "confirmed":
        ws.write_json(paths.confirmed(first), {
            "category": "тест", "cause": "Инженер проверил контракт ответа.",
            "fix": ["Обновить проверку контракта."], "extension": "sut-code", "details": [],
        })
    elif mode == "known":
        record = KBRecord(
            id="customer_missing_12345678", title="Покупатель не заполнен",
            category="service", description="Покупатель отсутствует в заказе.",
            resolution_steps=["Проверить покупателя до создания заказа."],
            error_example="java.lang.NullPointerException: customer is null",
        )
        for entry in run["clusters"]:
            record.confirm(entry["signature"], [])
            entry["kb"] = [{"id": record.id, "category": "приложение", "origin": "pattern"}]
            path = paths.analysis(entry["file_id"])
            path.write_text(path.read_text() + f"\nБАЗА ЗНАНИЙ: {record.id}\n")
        ProjectKB(Path(run["clusters"][0]["kb_dir"])).save(record)
        ws.write_json(paths.run_json, run)
    elif mode == "related":
        assert _next(run_dir, capsys).startswith("STATUS: relate")
        paths.related.write_text("ОДНА ПРИЧИНА: 1, 2 — Покупатель не заполнен в заказе\n")
        assert _next(run_dir, capsys).startswith("STATUS: summary")

    current = current_state(paths)
    _, report = render_report(
        current.run, current.analyses, current.flagged, "Итог прогона.", paths,
        known=current.known, related=current.related, confirmed=current.confirmed,
    )
    snapshot = extension_data.snapshot(paths, {})
    assert snapshot["groups"] == current.group_rows
    for item in snapshot["problems"]:
        diagnosis = current.diagnoses[f"{item['number']:02d}"]
        assert item["category"] == diagnosis.category
        assert item["cause"] == diagnosis.cause
        assert diagnosis.cause in report
    for item in snapshot["problems"]:
        diagnosis = current.diagnoses[f"{item['number']:02d}"]
        assert (item["confirmed"], item["feedback_unconfirmed"]) == (
            diagnosis.confirmed, diagnosis.feedback_unconfirmed)
    if mode == "feedback":
        assert current.diagnoses[first].feedback_unconfirmed
        assert not current.diagnoses[first].confirmed
        assert not current.diagnoses[first].accepted
        assert "На стенде исчерпан пул соединений" not in snapshot["problems"][0]["cause"]
        assert "подтверждено инженером" not in report
    elif mode == "remember":
        # Сохранённое remember исправление — подтверждённая причина и в отчёте, и в снимке.
        diagnosis = current.diagnoses[first]
        assert diagnosis.confirmed and diagnosis.accepted and not diagnosis.feedback_unconfirmed
        assert diagnosis.category == "окружение"
        assert snapshot["problems"][0]["cause"] == "На стенде исчерпан пул соединений."
        assert snapshot["problems"][0]["valid"]
        assert "(исправление инженера, сохранено в базе знаний" in report
        assert "проблема стенда или окружения — На стенде исчерпан пул соединений." in report
    elif mode == "confirmed":
        assert current.diagnoses[first].accepted and current.diagnoses[first].confirmed
    elif mode == "known":
        assert current.known.groups and current.group_rows[0]["numbers"] == [1, 2]
    elif mode == "related":
        assert current.related.groups and current.group_rows[0]["numbers"] == [1, 2]


def test_scoped_group_does_not_survive_absent_candidates(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    paths = ws.RunPaths(run_dir)
    assert _next(run_dir, capsys).startswith("STATUS: relate")
    paths.related.write_text("ОДНА ПРИЧИНА: 1, 2 — Покупатель не заполнен в заказе\n")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    assert ws.read_json(paths.state_json)["related_scopes"]
    for file_id in ids:
        ws.write_json(paths.confirmed(file_id), {
            "category": "приложение", "cause": "Инженер проверил отсутствие покупателя.",
            "fix": ["Проверить покупателя."], "extension": "sut-code", "details": [],
        })
    current = current_state(paths)
    assert not current.candidates
    assert not current.related.groups
    _next(run_dir, capsys)
    state = ws.read_json(paths.state_json)
    assert not {"related", "related_data_hash", "related_scopes"} & state.keys()


def test_validation_is_shared_without_changing_next_retry_state(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    paths = ws.RunPaths(run_dir)
    paths.analysis(ids[0]).write_text("ЧТО СЛОМАЛОСЬ: Тест упал без разбора.\n")
    before = paths.state_json.read_bytes() if paths.state_json.exists() else None
    current = current_state(paths)
    assert current.errors[ids[0]]
    assert ids[0] in current.flagged
    assert not current.diagnoses[ids[0]].accepted
    assert (paths.state_json.read_bytes() if paths.state_json.exists() else None) == before
    assert _next(run_dir, capsys).startswith("STATUS: fix")


@pytest.mark.parametrize("change", ["updated", "deleted", "rejected", "no_steps",
                                    "category", "missing", "manual"])
def test_readonly_auto_diagnosis_matches_next_after_record_changes(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], change: str,
) -> None:
    _remember_both(project, capsys)
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, run, output = _prepare(project, capsys, launch_id=778)
    assert output.startswith("STATUS: done"), output
    paths = ws.RunPaths(run_dir)
    entry = next(item for item in run["clusters"] if item.get("kb_auto")
                 and "customer" in paths.analysis(item["file_id"]).read_text())
    file_id = entry["file_id"]
    kb = ProjectKB(Path(entry["kb_dir"]))
    record = kb.get(entry["kb_auto"]["id"])
    assert record is not None
    if change == "updated":
        record.description = "Рецепт уточнён инженером после подготовки отчёта."
        record.resolution_steps = ["Проверить новый рецепт перед созданием заказа."]
        kb.save(record)
    elif change == "deleted":
        (Path(entry["kb_dir"]) / f"{record.id}.json").unlink()
    elif change == "rejected":
        record.reject(entry["signature"])
        kb.save(record)
    elif change == "no_steps":
        record.resolution_steps = []
        kb.save(record)
    elif change == "category":
        # Текущая запись проходит ту же проверку категории, что и next.
        record.category = "env"
        kb.save(record)
    elif change == "missing":
        paths.analysis(file_id).unlink()
    elif change == "manual":
        paths.analysis(file_id).write_text(VALID_ANALYSIS)
    before_state = paths.state_json.read_bytes()
    before_analysis = (paths.analysis(file_id).read_bytes()
                       if paths.analysis(file_id).exists() else None)
    current = current_state(paths)
    snapshot = extension_data.snapshot(paths, {})
    problem = next(item for item in snapshot["problems"] if item["number"] == int(file_id))
    assert problem["category"] == current.diagnoses[file_id].category
    assert problem["cause"] == current.diagnoses[file_id].cause
    assert paths.state_json.read_bytes() == before_state
    assert (paths.analysis(file_id).read_bytes() if paths.analysis(file_id).exists() else None
            ) == before_analysis

    result = _next(run_dir, capsys)
    after = current_state(paths)
    assert current.diagnoses[file_id] == after.diagnoses[file_id]
    if change in ("deleted", "rejected", "no_steps"):
        assert not problem["valid"] and file_id not in current.analyses
        assert result.startswith("STATUS: analyze"), result
        assert not paths.analysis(file_id).exists()
    else:
        assert file_id in current.analyses
        assert current.analyses[file_id].raw == paths.analysis(file_id).read_text().strip()
        if change == "updated":
            assert result.startswith("STATUS: done"), result
            assert record.description in problem["cause"]
            assert record.description in paths.report.read_text()
        elif change == "manual":
            assert file_id not in current.known.auto
        elif change == "category":
            assert problem["category"] == "окружение"
