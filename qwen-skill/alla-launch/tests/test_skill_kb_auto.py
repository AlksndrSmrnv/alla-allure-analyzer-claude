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


def _cluster(*messages: str, logs: tuple[str, ...] = ()) -> tuple[FailureCluster, dict[int, FailedTestSummary]]:
    tests = {
        number: make_failed_test_summary(
            test_result_id=number, name=f"test{number}", full_name=f"ru.company.OrderTest.test{number}",
            status_message=message, status_trace=message + "\n\tat ru.company.OrderTest.run(OrderTest.java:6)\n",
            log_snippet=logs[number - 1] if logs else None)
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
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    signature = cluster_signature(cluster, tests)
    evidence = "\n".join(part for part in cluster_evidence(cluster, tests) if part)
    matches = match_cluster(records, signature, evidence)
    return auto_resolution(matches, records, cluster, tests,
                           sources if sources is not None else _sources(POOL))


def test_exact_match_is_resolved_with_a_quote_of_its_fingerprint() -> None:
    cluster, tests = _cluster(POOL, POOL)
    record = _record(cluster_signature(cluster, tests))

    assert _decide(cluster, tests, [record]) == {"id": record.id, "quote": ["S1", POOL]}


@pytest.mark.parametrize("case", ["fingerprint_only", "two_exact", "no_steps", "other_member"])
def test_only_one_confirmed_record_for_every_member_resolves(case: str) -> None:
    other = "java.lang.NullPointerException: customer is null"
    cluster, tests = _cluster(POOL, POOL, other if case == "other_member" else POOL)
    signature = cluster_signature(cluster, tests)
    records = {
        "fingerprint_only": [_record(None)],
        "two_exact": [_record(signature), _record(signature, "pool_copy_9f8e7d6c")],
        "no_steps": [_record(signature, steps=[" "])],
        "other_member": [_record(signature)],
    }[case]

    # Тест с другой ошибкой — группа может быть смешанной (даже если он не пример задания):
    # однородность проверяет модель.
    assert _decide(cluster, tests, records) is None


def test_same_exception_with_other_errors_in_the_log_is_left_to_the_model() -> None:
    # Исключение называет причину само — лог в сигнатуру не входит, и у пула и NPE она одна.
    client = 'HttpServerErrorException$InternalServerError: 500 Internal Server Error: "submit failed"'
    pool = "2026-10-03 11:10:00 [ERROR] OrderRepository: HikariPool-7 - Connection is not available\n"
    npe = "2026-10-03 11:10:00 [ERROR] PromoService: NullPointerException: promo is null\n"
    cluster, tests = _cluster(client, client, logs=(pool, npe))
    record = _record(cluster_signature(cluster, tests), error_example="submit failed")
    assert cluster_signature(cluster.model_copy(update={"representative_test_id": 2}), tests) == \
        cluster_signature(cluster, tests)

    assert _decide(cluster, tests, [record], _sources(client)) is None
    same, same_tests = _cluster(client, client, logs=(pool, pool))
    assert _decide(same, same_tests, [_record(cluster_signature(same, same_tests),
                                               error_example="submit failed")], _sources(client))


def test_quote_falls_back_to_the_error_line_and_needs_text() -> None:
    cluster, tests = _cluster(POOL)
    record = _record(cluster_signature(cluster, tests), error_example="признака в данных нет")

    assert _decide(cluster, tests, [record]) == {"id": record.id, "quote": ["S1", POOL]}
    only_image = {"S1": {"kind": "image", "text": ""}}
    assert _decide(cluster, tests, [record], only_image) is None


def test_quote_without_guillemets_is_found_in_its_source() -> None:
    message = "Ошибка сервиса: «HikariPool-1 - Connection is not available» после 30 с"
    cluster, tests = _cluster(message)
    record = _record(cluster_signature(cluster, tests))
    sources = _sources(message)

    decision = _decide(cluster, tests, [record], sources)
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


# --- ответ инженера после отчёта -------------------------------------------------


def test_done_asks_once_which_analyses_are_right_and_saves_them_in_one_command(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    out = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    assert "«Какие разборы верны? Сохраню их в базу знаний" in out
    assert f"Проблемы: {int(order)}, {int(login)}." in out
    # Команда — с местом для верных номеров: готовая со всеми сохранила бы и неверный разбор.
    assert f"remember N1 N2 --run {run_dir} --from-analysis" in out
    assert f"«верны все» — {int(order)} {int(login)}." in out
    # Вопрос — первым после отчёта, до правок и перезапуска.
    after = out.split("После отчёта:", 1)[1]
    assert after.lstrip().startswith("- Сохранить разборы в базу знаний")

    code, out = _run(["remember", str(int(order)), str(int(login)), "--run", str(run_dir),
                      "--from-analysis"], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert "Запомнено в базе знаний проблем: 2 из 2." in out
    assert "напомни закоммитить папку alla-kb/" in out
    assert len(list((project / "alla-kb").glob("*.json"))) == 2
    # Сохранённое второй раз не спрашиваем.
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done") and "Какие разборы верны?" not in out


def test_remember_many_saves_what_it_can_and_names_the_rest(
    project: Path, testops: FakeTestOps, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    (run_dir / "analyses" / f"{login}.md").write_text("ПРИЧИНА: окружение — стенд\n", encoding="utf-8")

    code, out = _run(["remember", order, login, "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert "Запомнено в базе знаний проблем: 1 из 2." in out
    assert f"Не сохранены проблемы {int(login)}" in out and "без --from-analysis" in out
    assert f"- Проблема {int(login)}: разбор кластера не прошёл проверку формата" in out
    assert "Затем выполни" not in out  # команды отказа по одной проблеме не вкладываются

    # Не сохранилось ничего — отказ со списком замечаний, без команд.
    (run_dir / "analyses" / f"{order}.md").write_text("ПРИЧИНА: тест — ожидание\n", encoding="utf-8")
    code, out = _run(["remember", login, order, "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 1 and out.startswith("STATUS: error"), out
    assert "Затем выполни" not in out and out.count("- Проблема ") == 2

    code, out = _run(["remember", order, login, "--run", str(run_dir)], capsys)
    assert code == 1 and out.startswith("STATUS: error") and "только с --from-analysis" in out
    code, out = _run(["remember", order, "9", "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 1 and "нет проблем №9" in out


def _known_by_fingerprint(project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                          category: str = "service") -> tuple[Path, str, Path]:
    """Запись базы знаний, которая узнаёт проблему заказа только по признаку."""
    _, run, _ = _prepare(project, capsys)
    order = next(entry for entry in run["clusters"] if not entry["auto"])
    record = {
        "id": "order_npe_1a2b3c4d", "title": "NPE в OrderService", "category": category,
        "description": "Сервис заказов не проверяет customer.",
        "resolution_steps": ["Передавать customer"], "error_example": order["fingerprint"],
    }
    path = project / "alla-kb" / f"{record['id']}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    FakeTestOps(default_launch(778)).install(monkeypatch)
    run_dir, run2, _ = _prepare(project, capsys, launch_id=778)
    entry = next(e for e in run2["clusters"] if e["file_id"] == order["file_id"])
    assert [(m["id"], m["origin"]) for m in entry["kb"]] == [(record["id"], "fingerprint")]
    return run_dir, order["file_id"], path


def test_confirmed_analysis_of_a_known_problem_confirms_its_record(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, order, path = _known_by_fingerprint(project, capsys, monkeypatch)
    before = json.loads(path.read_text(encoding="utf-8"))
    login = next(e["file_id"] for e in json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["clusters"]
                 if not e["auto"] and e["file_id"] != order)
    _finish(run_dir, capsys, {order: VALID_ANALYSIS + f"БАЗА ЗНАНИЙ: {before['id']}\n",
                              login: MARKDOWN_ANALYSIS})

    code, out = _run(["remember", order, "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 0 and out.startswith("STATUS: saved"), out
    assert f"(запись подтверждена): {before['id']}" in out
    after = json.loads(path.read_text(encoding="utf-8"))
    entry = next(e for e in json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["clusters"]
                 if e["file_id"] == order)
    assert after["confirmed_signatures"] == [entry["signature"]] and after["confirmed_tests"]
    # Текст записи — слова инженера, а не разбора; дубля нет.
    assert {key: after[key] for key in ("title", "description", "resolution_steps", "error_example")} == {
        key: before[key] for key in ("title", "description", "resolution_steps", "error_example")}
    assert [p.name for p in (project / "alla-kb").glob("*.json")] == [path.name]


def test_known_record_with_another_category_is_not_confirmed(
    project: Path, testops: FakeTestOps, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, order, path = _known_by_fingerprint(project, capsys, monkeypatch, category="env")
    (run_dir / "analyses" / f"{order}.md").write_text(
        VALID_ANALYSIS + "БАЗА ЗНАНИЙ: order_npe_1a2b3c4d\n", encoding="utf-8")

    code, out = _run(["remember", order, "--run", str(run_dir), "--from-analysis"], capsys)
    assert code == 0 and out.startswith("STATUS: fix"), out
    assert "ссылается на известную проблему «order_npe_1a2b3c4d» с категорией «окружение»" in out
    assert json.loads(path.read_text(encoding="utf-8")).get("confirmed_signatures", []) == []


def test_question_lists_merged_problems_apart() -> None:
    from alla_skill_lib import cli
    from alla_skill_lib.workspace import RunPaths

    lines = cli._kb_question(RunPaths(Path("/r")), ["01", "03"], [["04", "05", "08"]])
    assert "Проблемы: 1, 3 и «Проблема 4 и 5 и 8»." in lines[0]
    assert any("remember N1 N2 --run /r --from-analysis" in line and "«верны все» — 1 3." in line
               for line in lines)
    assert any("«Проблема 4 и 5 и 8» (объединил агент) — не --from-analysis" in line for line in lines)
    assert cli._kb_question(RunPaths(Path("/r")), [], []) == []
