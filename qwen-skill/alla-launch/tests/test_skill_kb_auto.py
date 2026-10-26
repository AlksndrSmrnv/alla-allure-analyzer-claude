"""Точно узнанная проблема разбирается по записи базы знаний, без модели."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from skill_fixtures import (  # noqa: F401
    project_fixture,
    testops_fixture,
    without_libmagic,
)
from skill_factories import make_failed_test_summary
from skill_fake_testops import FakeTestOps, default_launch
from test_skill_flow import FEEDBACK, MARKDOWN_ANALYSIS, VALID_ANALYSIS, _finish, _next, _prepare, _run

from alla_core.models.clustering import ClusterSignature, FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.kb import KBRecord, cluster_evidence, cluster_signature, match_cluster
from alla_skill_lib.known_issues import KB_AUTO_STATE, auto_resolution, known_issue_analysis

POOL = "java.lang.IllegalStateException: HikariPool-1 - Connection is not available"
TRACE = POOL + "\n\tat ru.company.orders.OrderTest.create(OrderTest.java:6)\n"


def _cluster(*messages: str) -> tuple[FailureCluster, dict[int, FailedTestSummary]]:
    tests = {
        number: make_failed_test_summary(
            test_result_id=number, name=f"test{number}", full_name=f"ru.company.OrderTest.test{number}",
            status_message=message, status_trace=message + "\n\tat ru.company.OrderTest.run(OrderTest.java:6)\n")
        for number, message in enumerate(messages, start=1)
    }
    cluster = FailureCluster(
        cluster_id="c1", label="l", signature=ClusterSignature(), member_test_ids=list(tests),
        member_count=len(tests), representative_test_id=1, example_message=messages[0],
    )
    return cluster, tests


def _record(signature: str | None, entry_id: str = "pool_1a2b3c4d", **kwargs: Any) -> KBRecord:
    return KBRecord(
        id=entry_id, title=kwargs.pop("title", "Пул соединений БД исчерпан"), category="service",
        description=kwargs.pop("description", "Сервис заказов не получает соединение из пула."),
        resolution_steps=kwargs.pop("steps", ["Увеличить пул", "Найти утечку соединений"]),
        error_example=kwargs.pop("error_example", "HikariPool-1 - Connection is not available"),
        confirmed_signatures=[signature] if signature else [], **kwargs,
    )


def _sources(message: str) -> dict[str, dict[str, Any]]:
    return {"S1": {"kind": "message", "text": message}, "S2": {"kind": "trace", "text": TRACE}}


def _decide(
    cluster: FailureCluster,
    tests: dict[int, FailedTestSummary],
    records: list[KBRecord],
    examples: list[int],
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    signature = cluster_signature(cluster, tests)
    evidence = "\n".join(part for part in cluster_evidence(cluster, tests) if part)
    matches = match_cluster(records, signature, evidence)
    return auto_resolution(matches, records, cluster, tests,
                           [{"role": "typical", "test_result_id": number} for number in examples],
                           sources if sources is not None else _sources(POOL))


def test_exact_match_is_resolved_with_a_quote_of_its_fingerprint() -> None:
    cluster, tests = _cluster(POOL, POOL)
    record = _record(cluster_signature(cluster, tests))

    assert _decide(cluster, tests, [record], [1, 2]) == {"id": record.id, "quote": ["S1", POOL]}


@pytest.mark.parametrize("case", ["fingerprint_only", "two_exact", "no_steps", "other_example"])
def test_only_one_confirmed_record_for_every_example_resolves(case: str) -> None:
    other = "java.lang.NullPointerException: customer is null"
    cluster, tests = _cluster(POOL, other if case == "other_example" else POOL)
    signature = cluster_signature(cluster, tests)
    records = {
        "fingerprint_only": [_record(None)],
        "two_exact": [_record(signature), _record(signature, "pool_copy_9f8e7d6c")],
        "no_steps": [_record(signature, steps=[" "])],
        "other_example": [_record(signature)],
    }[case]

    # Пример с другой ошибкой — группа может быть смешанной: однородность проверяет модель.
    assert _decide(cluster, tests, records, [1, 2]) is None


def test_quote_falls_back_to_the_error_line_and_needs_text() -> None:
    cluster, tests = _cluster(POOL)
    record = _record(cluster_signature(cluster, tests), error_example="признака в данных нет")

    assert _decide(cluster, tests, [record], [1]) == {"id": record.id, "quote": ["S1", POOL]}
    only_image = {"S1": {"kind": "image", "text": ""}}
    assert _decide(cluster, tests, [record], [1], only_image) is None


def test_quote_without_guillemets_is_found_in_its_source() -> None:
    message = "Ошибка сервиса: «HikariPool-1 - Connection is not available» после 30 с"
    cluster, tests = _cluster(message)
    record = _record(cluster_signature(cluster, tests))
    sources = _sources(message)

    decision = _decide(cluster, tests, [record], [1], sources)
    assert decision is not None and "«" not in decision["quote"][1]
    text = known_issue_analysis(record, (decision["quote"][0], decision["quote"][1]))
    analysis = parse_analysis(text)
    assert validate_analysis(analysis, Path("."), frozenset({record.id}), observed=True,
                             sources=sources, exact_kb={record.id: "приложение"}) == []


def test_analysis_from_a_record_passes_the_usual_check() -> None:
    cluster, tests = _cluster(POOL, POOL)
    record = _record(cluster_signature(cluster, tests))

    text = known_issue_analysis(record, ("S1", POOL), examples=2)
    analysis = parse_analysis(text)
    assert validate_analysis(analysis, Path("."), frozenset({record.id}), observed=True,
                             sources=_sources(POOL), examples=2,
                             exact_kb={record.id: "приложение"}) == []
    assert analysis.category == "приложение" and analysis.kb_ref == record.id
    assert analysis.consistency_kind == "same"
    assert analysis.cause_reason == record.description
    assert analysis.fix.splitlines() == ["1. Увеличить пул", "2. Найти утечку соединений"]


# --- сквозные сценарии -------------------------------------------------------


def _remember_both(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str, str]:
    """Первый прогон: инженер подтвердил обе проблемы с данными."""
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    (run_dir / "feedback" / f"{order}.md").write_text(FEEDBACK, encoding="utf-8")
    assert _run(["remember", order, "--run", str(run_dir)], capsys)[1].startswith("STATUS: saved")
    out = _run(["remember", login, "--run", str(run_dir), "--from-analysis"], capsys)[1]
    assert out.startswith("STATUS: saved"), out
    return run_dir, order, login


def test_all_known_problems_finish_without_the_model(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _remember_both(project, capsys)

    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, run, out = _prepare(project, capsys, launch_id=778)

    # Все проблемы с данными известны: ни разборов, ни сводки модель не пишет.
    assert out.startswith("STATUS: done"), out
    assert sum(1 for entry in run["clusters"] if entry.get("kb_auto")) == 2
    assert not (run_dir / "summary_task.md").exists()
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("Все проблемы прогона с данными об ошибке уже известны")
    assert "(2 проблемы). Ещё у 1 проблемы нет данных об ошибке" in summary
    assert "Что делать: Передавать customer в запросе создания заказа." in summary
    assert "По базе знаний без агента: 2 известные проблемы" in out
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert len(state[KB_AUTO_STATE]) == 2


def test_updated_record_rewrites_the_analysis_and_a_hand_edit_stays(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, order, login = _remember_both(project, capsys)
    entry_id = next(path.stem for path in (project / "alla-kb").glob("*.json")
                    if "customer" in path.read_text(encoding="utf-8"))
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, _, out = _prepare(project, capsys, launch_id=778)
    assert out.startswith("STATUS: done"), out

    # Инженер уточнил рецепт записи — разбор по ней переписывается, сводка тоже.
    (run_dir / "feedback" / f"{order}.md").write_text(
        FEEDBACK.replace("Передавать customer", "Заполнять customer"), encoding="utf-8")
    code, out = _run(["remember", order, "--entry", entry_id, "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: saved") and f"next {run_dir}" in out, out
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    analysis = (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8")
    assert "1. Заполнять customer в запросе создания заказа" in analysis
    assert "Заполнять customer" in (run_dir / "summary.md").read_text(encoding="utf-8")

    # Разбор, который написал не скрипт, не перезаписывается — это уже разбор агента.
    edited = VALID_ANALYSIS + f"БАЗА ЗНАНИЙ: {entry_id}\n"
    (run_dir / "analyses" / f"{order}.md").write_text(edited, encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: summary"), out  # уже не все проблемы разобраны по записям
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done"), out
    assert (run_dir / "analyses" / f"{order}.md").read_text(encoding="utf-8") == edited
    assert "По базе знаний без агента: 1 известная проблема" in out


def test_fingerprint_match_is_analysed_by_the_model(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, order, _ = _remember_both(project, capsys)
    for path in (project / "alla-kb").glob("*.json"):  # подтверждённых сигнатур и тестов нет
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "confirmed_signatures": [], "confirmed_tests": []}),
                        encoding="utf-8")

    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, run, out = _prepare(project, capsys, launch_id=778)
    entry = next(e for e in run["clusters"] if e["file_id"] == order)
    assert [m["origin"] for m in entry["kb"]] == ["fingerprint"] and "kb_auto" not in entry
    assert out.startswith("STATUS: analyze") and "по базе знаний" not in out
    assert "ПРИЗНАК НАЙДЕН" in (run_dir / "clusters" / f"{order}.md").read_text(encoding="utf-8")


def test_review_counts_analyses_from_records_as_script_work(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from alla_skill_lib import review
    from alla_skill_lib import workspace as ws
    from alla_skill_lib.review_model import select_problems

    _remember_both(project, capsys)
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, _, _ = _prepare(project, capsys, launch_id=778)

    facts = review.collect_facts(ws.RunPaths(run_dir))
    assert facts["size"]["manual"] == 0 and facts["size"]["kb_auto"] == 2
    assert facts["quality"]["analysed"] == 0 and select_problems(facts) == []
    assert "из них по базе знаний — 2" in review.render_report(facts)
