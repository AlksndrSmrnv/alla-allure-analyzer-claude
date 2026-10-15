"""Лог операции теста в задании: свой лог под своим тестом, «лог операции не найден»."""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_fixtures import project_fixture, without_libmagic  # noqa: F401
from test_skill_flow import _prepare, _run

from alla_core.config import Settings
from alla_core.models.clustering import ClusterExample, ClusterSignature, FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib.analysis_format import LOG_NOT_FOUND_ERROR, parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import (
    LOG_NOT_FOUND,
    build_cluster_task_with_sources,
    build_task_text,
    log_note_for,
)
from eval.cassette import replay
from eval.corpus_dev import shared_service_log


def _task_by_message(run_dir: Path, run: dict, word: str) -> tuple[dict, str]:
    for entry in run["clusters"]:
        if word in entry["label"]:
            return entry, (run_dir / "clusters" / f"{entry['file_id']}.md").read_text(
                encoding="utf-8")
    raise AssertionError(f"нет проблемы с «{word}»")


def test_shared_log_gives_each_test_only_its_operation(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = shared_service_log()
    with replay(case.fixture):
        run_dir, run, _ = _prepare(project, capsys, launch_id=case.fixture.launch["id"])

    assert len(run["clusters"]) == 3
    orders, task = _task_by_message(run_dir, run, "ORD-1")
    assert "Операция теста: operUID=op-ord-100" in task
    assert "Discount rule DR-5 not found" in task
    assert "DECLINED" not in task and "Корреляция запроса" not in task
    assert not orders["log_not_found"]

    loyalty, task = _task_by_message(run_dir, run, "Loyalty")
    assert loyalty["log_not_found"]
    assert f"[{LOG_NOT_FOUND}: id операции теста" in task and "shop-service.log" in task
    assert "DR-5" not in task and "DECLINED" not in task
    assert f"Начни с «{LOG_NOT_FOUND}, анализ построен по ошибке теста»" in " ".join(task.split())


def test_analysis_without_the_operation_log_in_missing_is_sent_back(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = shared_service_log()
    with replay(case.fixture):
        run_dir, run, _ = _prepare(project, capsys, launch_id=case.fixture.launch["id"])
    loyalty, _ = _task_by_message(run_dir, run, "Loyalty")
    text = ("ЧТО СЛОМАЛОСЬ: Лог операции не найден, анализ построен по ошибке теста: баллы "
            "не начислены.\nПРИЧИНА: неизвестно — по ошибке теста причину не установить.\n"
            "НЕ ХВАТАЕТ: {missing}\nКАК ИСПРАВИТЬ:\n1. Найти запись операции.\n")
    analysis = run_dir / "analyses" / f"{loyalty['file_id']}.md"
    analysis.write_text(text.format(missing="ответа сервиса баллов"), encoding="utf-8")

    code, out = _run(["verify", loyalty["file_id"], "--run", str(run_dir)], capsys)
    assert code == 0 and out.startswith("STATUS: fix")
    assert LOG_NOT_FOUND_ERROR in " ".join(out.split())

    errors = validate_analysis(
        parse_analysis(text.format(missing="лога сервиса баллов за время теста с его operUID")),
        Path("."), observed=True, sources={}, log_not_found=True)
    assert errors == []


def test_log_of_another_member_is_its_own_example() -> None:
    tests = {
        1: FailedTestSummary(test_result_id=1, name="first", status="failed",
                             status_message="Order ORD-1 was not created",
                             correlation_hint="operUID=op-1", operation_ids=["operUID=op-1"],
                             operation_logs=["service.log"], foreign_log_events=2),
        2: FailedTestSummary(test_result_id=2, name="second", status="failed",
                             status_message="Order ORD-2 was not created",
                             correlation_hint="operUID=op-2",
                             log_snippet="--- [файл: service.log] ---\n[строка 3]\n"
                                         "2026-10-03 10:00:00 [operUID=op-2] ERROR stock timeout"),
    }
    cluster = FailureCluster(
        cluster_id="c", label="Order was not created", signature=ClusterSignature(),
        member_test_ids=[1, 2], member_count=2, representative_test_id=1,
        example_message=tests[1].status_message,
        examples=[ClusterExample(role="typical", test_result_id=1)])
    task = build_cluster_task_with_sources(
        cluster=cluster, position=1, total=1, launch_id=1, answer_path="/a.md",
        next_command="next", tests_by_id=tests, log_snippet=tests[2].log_snippet,
        full_trace=None, frames=[], hints=[], settings=Settings())

    assert [shown["role"] for shown in task.examples] == ["typical", "log"]
    assert task.blocks == 2 and not task.log_not_found
    text = task.text
    first, second = text.split("### Пример 2")
    assert "Операция теста: operUID=op-1" in first and LOG_NOT_FOUND in first
    assert "stock timeout" not in first
    assert "Операция теста: operUID=op-2" in second and "stock timeout" in second
    assert "Order ORD-2 was not created" in second


def test_log_note_only_when_an_operation_log_gave_nothing() -> None:
    own = FailedTestSummary(test_result_id=1, name="t", status="failed",
                            operation_logs=["service.log"], operation_ids=["rqUID=r-1"],
                            foreign_log_events=3)
    assert log_note_for(own) == (f"[{LOG_NOT_FOUND}: в service.log нет ошибок операции этого "
                                 "теста (rqUID=r-1); ошибки других операций (3) не показаны]")
    own.log_snippet = "--- [файл: service.log] ---\nown error"
    assert log_note_for(own) is None
    plain = FailedTestSummary(test_result_id=2, name="t", status="failed")
    assert log_note_for(plain) is None


def test_task_variant_for_a_missing_operation_log() -> None:
    task = " ".join(build_task_text(has_symptom=True, has_log=False, low_evidence=False,
                                    has_kb=False, log_not_found=True).split())
    assert f"Начни с «{LOG_NOT_FOUND}, анализ построен по ошибке теста»" in task
    assert "Лог операции этого теста не найден — в «НЕ ХВАТАЕТ» назови его" in task
    assert "лога приложения в данных нет" not in task
