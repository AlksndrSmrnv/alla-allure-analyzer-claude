"""Несколько примеров в задании: блоки данных, общие id, лимиты и «СОГЛАСОВАННОСТЬ»."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from test_skill_flow import _next, _prepare

from alla_core.config import Settings
from alla_core.models.clustering import ClusterExample, ClusterSignature, FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import build_cluster_task_with_sources, build_task_text
from eval.cassette import replay
from eval.corpus_dev import same_assertion_db_vs_npe

HEAD = ("ЧТО СЛОМАЛОСЬ: Тесты получили 500.\nПРИЧИНА: приложение — сервис падает.\n"
        "НАБЛЮДЕНИЯ:\n- [S1] «expected: <200> but was: <500>»\n")
TAIL = "КАК ИСПРАВИТЬ:\n1. Починить сервис.\n"
SOURCES = {"S1": {"kind": "message", "text": "expected: <200> but was: <500>"}}


def _errors(consistency: str, examples: int = 2) -> list[str]:
    text = HEAD + consistency + TAIL
    return validate_analysis(parse_analysis(text), Path("."), task_format=2, sources=SOURCES,
                             examples=examples)


@pytest.mark.parametrize(("line", "kind"), [
    ("СОГЛАСОВАННОСТЬ: одна причина\n", "same"),
    ("СОГЛАСОВАННОСТЬ: разные проблемы — у одного теста пул БД, у другого NPE\n", "different"),
    ("**Согласованность:** недостаточно данных — у второго примера нет лога\n", "insufficient"),
])
def test_consistency_values(line: str, kind: str) -> None:
    assert _errors(line) == []
    assert parse_analysis(HEAD + line + TAIL).consistency_kind == kind


@pytest.mark.parametrize(("line", "message"), [
    ("", "добавь «СОГЛАСОВАННОСТЬ:»"),
    ("СОГЛАСОВАННОСТЬ: вроде одинаково\n", "напиши одно из"),
    ("СОГЛАСОВАННОСТЬ: разные проблемы\n", "назови, чем отличаются примеры"),
])
def test_consistency_errors(line: str, message: str) -> None:
    assert any(message in error for error in _errors(line))


def test_consistency_is_needed_only_with_several_examples() -> None:
    assert _errors("", examples=1) == []
    legacy = validate_analysis(parse_analysis(HEAD + TAIL), Path("."), examples=3)
    assert legacy == []  # формат 1: правил наблюдений и согласованности нет


def test_task_asks_for_consistency_only_with_several_examples() -> None:
    single = build_task_text(has_symptom=True, has_log=True, low_evidence=False, has_kb=False)
    several = build_task_text(has_symptom=True, has_log=True, low_evidence=False, has_kb=False,
                              examples=2)
    assert "СОГЛАСОВАННОСТЬ" not in single
    line = "СОГЛАСОВАННОСТЬ: одна причина | разные проблемы — <чем отличаются примеры> | " \
           "недостаточно данных"
    assert line in several.splitlines()
    text = " ".join(several.split())
    for phrase in ("сравни их", "ПРИЧИНУ пиши по первому (типичному) примеру",
                   "Наблюдения — из любого примера, с id его куска"):
        assert phrase in text


def test_merged_cluster_shows_both_server_errors_and_needs_consistency(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = same_assertion_db_vs_npe()
    with replay(case.fixture):
        run_dir, run, _ = _prepare(project, capsys, launch_id=case.fixture.launch["id"])
    entry, = run["clusters"]
    assert entry["example_blocks"] == 2
    assert [example["role"] for example in entry["examples"]] == ["typical", "different"]
    task = (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")
    assert "Примеров в данных: 2 (типичный, наиболее отличающийся)" in task
    assert "### Пример 1 — типичный · тест " in task and "### Пример 2 — наиболее отличающийся" in task
    assert 'Cannot invoke "Discount.percent()"' in task and "HikariPool-1" in task
    assert "Сообщение об ошибке — такое же, как в примере 1." in task
    sources = json.loads((run_dir / "evidence" / "01.sources.json").read_text("utf-8"))
    tests = {record["test_result_id"] for record in sources.values()}
    assert tests == {example["test_result_id"] for example in entry["examples"]}

    log_ids = [key for key, record in sources.items() if record["kind"] == "log"]
    quotes = [sources[key]["text"].splitlines()[1][:60] for key in log_ids]
    base = (
        "ЧТО СЛОМАЛОСЬ: Тесты создания заказа получили 500 вместо 200.\n"
        "ПРИЧИНА: приложение — сервис заказов падает.\nНАБЛЮДЕНИЯ:\n"
        + "".join(f"- [{key}] «{quote}»\n" for key, quote in zip(log_ids, quotes))
        + "НЕ ХВАТАЕТ: нет\n{consistency}КАК ИСПРАВИТЬ:\n1. Починить сервис заказов.\n"
    )
    analysis = run_dir / "analyses" / "01.md"
    analysis.write_text(base.format(consistency=""), encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: fix") and "добавь «СОГЛАСОВАННОСТЬ:»" in out
    assert "СОГЛАСОВАННОСТЬ ✗" in out
    analysis.write_text(base.format(
        consistency="СОГЛАСОВАННОСТЬ: разные проблемы — у одного теста пул БД, у другого NPE.\n"),
        encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: summary")


def _big_test(test_id: int, error: str) -> FailedTestSummary:
    noise = "\n\n".join(f"[строка {n}]\n2026-10-03 10:00:00 [ERROR] {error} {n} " + "x" * 300
                        for n in range(1, 120))
    return FailedTestSummary(
        test_result_id=test_id, name=f"test{test_id}", status="failed",
        status_message=f"expected: <200> but was: <500> {error} " + "m" * 3000,
        status_trace="java.lang.AssertionError\n" + "\tat a.B.c(B.java:1)\n" * 200,
        failed_step_path="step", log_snippet="--- [файл: app.log] ---\n" + noise)


def test_three_big_examples_stay_within_the_old_limits_plus_headers() -> None:
    tests = {i: _big_test(i, error) for i, error in
             ((1, "PoolTimeout"), (2, "NullPointer"), (3, "Deadlock"))}
    roles = ["typical", "different", "informative"]
    common = dict(position=1, total=1, launch_id=1, answer_path="/a.md", next_command="next",
                  tests_by_id=tests, full_trace=tests[1].status_trace, frames=[], hints=[],
                  settings=Settings())

    def cluster(count: int) -> FailureCluster:
        return FailureCluster(
            cluster_id="c", label="x", signature=ClusterSignature(), member_test_ids=[1, 2, 3],
            member_count=3, representative_test_id=1,
            example_message=tests[1].status_message, example_step_path="step",
            examples=[ClusterExample(role=role, test_result_id=i)
                      for i, role in zip(range(1, count + 1), roles)])

    one = build_cluster_task_with_sources(cluster=cluster(1), log_snippet=tests[1].log_snippet,
                                          **common)
    three = build_cluster_task_with_sources(cluster=cluster(3), log_snippet=tests[1].log_snippet,
                                            **common)
    assert three.blocks == 3 and one.blocks == 1
    # Лимиты сообщения, трейса и лога делятся между примерами; сверху — заголовки блоков,
    # пометки пропусков и текст про «СОГЛАСОВАННОСТЬ».
    assert len(three.text) <= len(one.text) + 2500, (len(one.text), len(three.text))
    assert {source.test_result_id for source in three.sources} == {1, 2, 3}


def _mixed_run(project: Path, capsys: pytest.CaptureFixture[str], consistency: str,
               category: str = "тест") -> tuple[Path, str]:
    case = same_assertion_db_vs_npe()
    with replay(case.fixture):
        run_dir, _run, _ = _prepare(project, capsys, launch_id=case.fixture.launch["id"])
    sources = json.loads((run_dir / "evidence" / "01.sources.json").read_text("utf-8"))
    log_id = next(key for key, record in sources.items() if record["kind"] == "log")
    quote = sources[log_id]["text"].splitlines()[1][:60]
    (run_dir / "analyses" / "01.md").write_text(
        "ЧТО СЛОМАЛОСЬ: Тесты создания заказа получили 500 вместо 200.\n"
        f"ПРИЧИНА: {category} — тест ждёт 200 при сбое сервиса.\n"
        f"НАБЛЮДЕНИЯ:\n- [{log_id}] «{quote}»\nНЕ ХВАТАЕТ: нет\n"
        f"СОГЛАСОВАННОСТЬ: {consistency}\n"
        "КАК ИСПРАВИТЬ:\n1. Разобрать падения по отдельности.\n"
        "КОД: src/test/java/ru/company/orders/OrderTest.java:6 — assertEquals(200, …)\n",
        encoding="utf-8")
    return run_dir, _next(run_dir, capsys)


def test_mixed_group_is_flagged_and_gets_no_common_fix(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = "у одного теста исчерпан пул БД, у другого NPE в DiscountService"
    run_dir, out = _mixed_run(project, capsys, f"разные проблемы — {detail}")
    # Категория «тест» с КОД, но правку для неоднородной группы не предлагают.
    assert out.startswith("STATUS: summary"), out
    summary_task = (run_dir / "summary_task.md").read_text(encoding="utf-8")
    assert f"СОГЛАСОВАННОСТЬ: разные проблемы — {detail}" in summary_task
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    out = _next(run_dir, capsys)
    assert out.startswith("STATUS: done") and "apply 1" not in out
    brief = out.split("===ОТЧЁТ===\n", 1)[1].split("\n===КОНЕЦ===", 1)[0]
    assert "### Требуют вашего внимания (1)" in brief
    assert "в группе, похоже, несколько проблем" in brief
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert f"- В группе, похоже, несколько проблем: {detail}" in report
    assert f"- **В группе, похоже, несколько проблем:** {detail}" in report


def test_unchecked_group_is_marked_and_one_cause_keeps_the_usual_flow(
    project: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_dir, out = _mixed_run(project, capsys, "недостаточно данных — у второго примера нет "
                                               "кода ответа", category="приложение")
    assert out.startswith("STATUS: summary")
    (run_dir / "summary.md").write_text("Итог.", encoding="utf-8")
    _next(run_dir, capsys)
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "- Однородность группы не проверена: недостаточно данных, чтобы сравнить примеры" in report

    _run_dir, out = _mixed_run(project, capsys, "одна причина")
    assert out.startswith("STATUS: propose")  # обычная группа «тест» с КОД — правку предлагают
