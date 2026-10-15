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


_APP = ("ЧТО СЛОМАЛОСЬ: Заказ не создан: сервис ответил 500.\n"
        "ПРИЧИНА: {category} — сервис заказов падает на пустом покупателе.\n"
        "НАБЛЮДЕНИЯ:\n{observations}"
        "НЕ ХВАТАЕТ: нет\nКАК ИСПРАВИТЬ:\n1. Заполнить покупателя в заказе.\n{kb}")
_SOURCES = {
    "S1": {"kind": "message", "text": "expected: <200> but was: <500>"},
    "S2": {"kind": "log", "section": "HTTP", "text": "HTTP статус: 500\nerror: customer is null"},
    "S3": {"kind": "log", "section": "лог", "text": "ERROR NullPointerException: customer is null"},
}


def _category_errors(category: str, observations: str, kb: str = "",
                     exact_kb: dict[str, str] | None = None) -> list[str]:
    text = _APP.format(category=category, observations=observations, kb=kb)
    return validate_analysis(parse_analysis(text), Path("."), frozenset(exact_kb or {}),
                             observed=True, sources=_SOURCES, exact_kb=exact_kb)


@pytest.mark.parametrize("category", ["приложение", "данные"])
def test_app_and_data_need_a_quote_from_the_operation_log(category: str) -> None:
    from_message = "- [S1] «expected: <200> but was: <500>»\n"
    from_http = "- [S2] «error: customer is null»\n"
    from_log = "- [S3] «NullPointerException: customer is null»\n"
    error = f"категорию «{category}» подтверждают только цитата из лога операции"

    assert any(error in item for item in _category_errors(category, from_message))
    # Ответ HTTP — то, что увидел тест, а не лог сервиса.
    assert any(error in item for item in _category_errors(category, from_http))
    assert _category_errors(category, from_message + from_log) == []
    # Точная (подтверждённая) запись базы знаний той же категории тоже подтверждает.
    assert _category_errors(category, from_message, "БАЗА ЗНАНИЙ: orders_npe_1a2b\n",
                            {"orders_npe_1a2b": category}) == []
    other = "данные" if category == "приложение" else "приложение"
    assert any(error in item for item in _category_errors(
        category, from_message, "БАЗА ЗНАНИЙ: orders_npe_1a2b\n", {"orders_npe_1a2b": other}))


@pytest.mark.parametrize("category", ["тест", "окружение"])
def test_other_categories_do_not_need_the_log(category: str) -> None:
    assert _category_errors(category, "- [S1] «expected: <200> but was: <500>»\n") == []


def test_lost_registry_does_not_block_the_category() -> None:
    text = _APP.format(category="приложение", kb="",
                       observations="- [S1] «expected: <200> but was: <500>»\n")
    assert validate_analysis(parse_analysis(text), Path("."), observed=True, sources=None) == []


def test_http_response_does_not_replace_the_operation_log() -> None:
    """Ответ с ошибкой 500 — то, что видел тест: пометка и требование к «НЕ ХВАТАЕТ» остаются."""
    test = FailedTestSummary(
        test_result_id=1, name="createOrder", status="failed",
        status_message="Order ORD-1 was not created: status code 500",
        correlation_hint="operUID=op-1", operation_ids=["operUID=op-1"],
        operation_logs=["service.log"], foreign_log_events=2,
        log_snippet="--- [HTTP: Response] ---\nHTTP статус: 500\nerror: Internal Server Error")
    cluster = FailureCluster(
        cluster_id="c", label="Order was not created", signature=ClusterSignature(),
        member_test_ids=[1], member_count=1, representative_test_id=1,
        example_message=test.status_message,
        examples=[ClusterExample(role="typical", test_result_id=1)])
    task = build_cluster_task_with_sources(
        cluster=cluster, position=1, total=1, launch_id=1, answer_path="/a.md",
        next_command="next", tests_by_id={1: test}, log_snippet=test.log_snippet,
        full_trace=None, frames=[], hints=[], settings=Settings())

    assert task.log_not_found
    assert "HTTP статус: 500" in task.text and f"[{LOG_NOT_FOUND}: в service.log" in task.text
    text = " ".join(task.text.split())
    assert "Лог операции этого теста не найден — в «НЕ ХВАТАЕТ» назови его" in text
    # Вариант задания — «лога нет»: HTTP-ответ — симптом, а не лог сервиса.
    assert f"Начни с «{LOG_NOT_FOUND}, анализ построен по ошибке теста»" in text
    assert "500 и другие 5xx — «неизвестно»" in text


def test_http_response_alone_is_a_symptom_not_an_application_log() -> None:
    test = FailedTestSummary(
        test_result_id=1, name="createOrder", status="failed",
        log_snippet="--- [HTTP: Response] ---\nHTTP статус: 500\nerror: Internal Server Error")
    cluster = FailureCluster(
        cluster_id="c", label="Тест: createOrder", signature=ClusterSignature(),
        member_test_ids=[1], member_count=1, representative_test_id=1,
        examples=[ClusterExample(role="typical", test_result_id=1)])
    task = build_cluster_task_with_sources(
        cluster=cluster, position=1, total=1, launch_id=1, answer_path="/a.md",
        next_command="next", tests_by_id={1: test}, log_snippet=test.log_snippet,
        full_trace=None, frames=[], hints=[], settings=Settings())

    text = " ".join(task.text.split())
    assert "анализ построен по логу приложения" not in text
    assert "лога приложения в данных нет" in text and not task.log_not_found


def test_http_response_beside_a_service_log_is_the_symptom() -> None:
    """Сообщения и трейса нет: HTTP-код — симптом, лог сервиса — причина, их надо связать."""
    test = FailedTestSummary(
        test_result_id=1, name="createOrder", status="failed",
        log_snippet=("--- [файл: service.log] ---\n[строка 3]\n"
                     "2026-10-03 10:00:00 [operUID=op-1] ERROR NullPointerException: customer\n\n"
                     "--- [HTTP: Response] ---\nHTTP статус: 500"))
    cluster = FailureCluster(
        cluster_id="c", label="Тест: createOrder", signature=ClusterSignature(),
        member_test_ids=[1], member_count=1, representative_test_id=1,
        examples=[ClusterExample(role="typical", test_result_id=1)])
    task = build_cluster_task_with_sources(
        cluster=cluster, position=1, total=1, launch_id=1, answer_path="/a.md",
        next_command="next", tests_by_id={1: test}, log_snippet=test.log_snippet,
        full_trace=None, frames=[], hints=[], settings=Settings())

    text = " ".join(task.text.split())
    assert "Первое — симптом из ошибки или трейса" in text  # вариант «симптом + лог»
    assert "сообщения об ошибке и стек-трейса нет, анализ построен по логу" not in text
