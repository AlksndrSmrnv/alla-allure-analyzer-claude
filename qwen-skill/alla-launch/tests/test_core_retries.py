"""Повторы (шаг 5): связь hidden-попыток с финальными результатами."""

from __future__ import annotations

from typing import Any

import pytest

from skill_fixtures import without_libmagic  # noqa: F401
from alla_core.config import Settings
from alla_core.models.testops import LaunchResponse, RetryInfo
from alla_core.models.testops import TestResultResponse as ResultResponse
from alla_core.services.retry_linking import (
    CONTEXT_KEY,
    link_attempts,
    retry_warnings,
)
from alla_core.services.triage_service import TriageService


def _result(result_id: int, status: str = "failed", *, hidden: bool = False,
            **extra: Any) -> ResultResponse:
    payload: dict[str, Any] = {"id": result_id, "name": f"test{result_id}", "status": status}
    if hidden:
        payload["hidden"] = True
    payload.update(extra)
    return ResultResponse.model_validate(payload)


def _context(case_id: int, browser: str = "chrome", stand: str = "stage-1") -> dict[str, Any]:
    return {
        "testCaseId": case_id,
        "parameters": [{"name": "browser", "value": browser}],
        "environment": [{"name": "stand", "value": stand}],
    }


def _linked(links: Any) -> dict[int, list[int]]:
    return {final: [attempt.id for attempt in attempts]
            for final, attempts in links.attempts.items()}


def test_links_by_history_id_in_creation_order() -> None:
    results = [
        _result(1, hidden=True, historyId="h-a", createdDate=300),
        _result(2, hidden=True, historyId="h-a", createdDate=100),
        _result(3, historyId="h-a", createdDate=400),
        _result(4, "passed", historyId="h-b"),
    ]

    links = link_attempts(results)

    assert links.linked_by == "historyId"
    assert _linked(links) == {3: [2, 1]}
    assert links.info() == RetryInfo(linked_by="historyId", hidden_total=2, linked=2)


def test_history_key_is_used_when_history_id_is_absent() -> None:
    links = link_attempts([_result(1, hidden=True, historyKey="k"), _result(2, historyKey="k")])

    assert links.linked_by == "historyKey"
    assert _linked(links) == {2: [1]}


def test_parametrized_test_with_one_test_case_is_not_mixed() -> None:
    results = [
        _result(1, hidden=True, **_context(504, "chrome")),
        _result(2, hidden=True, **_context(504, "firefox")),
        _result(3, **_context(504, "firefox")),
        _result(4, **_context(504, "chrome")),
    ]

    links = link_attempts(results)

    assert links.linked_by == CONTEXT_KEY
    assert _linked(links) == {4: [1], 3: [2]}


def test_environment_change_is_not_mixed() -> None:
    results = [_result(1, hidden=True, **_context(505, stand="stage-1")),
               _result(2, **_context(505, stand="stage-2"))]

    links = link_attempts(results)

    assert _linked(links) == {}
    assert (links.linked, links.no_final) == (0, 1)


@pytest.mark.parametrize("context", [
    {"parameters": None, "environment": None},
    {"parameters": [{"name": "browser", "value": "chrome"}]},
    {"environment": [{"name": "stand", "value": "stage-1"}]},
    {"parameters": [], "environment": None},
    {"parameters": "chrome", "environment": []},
])
def test_unknown_context_is_not_a_link_key(context: dict[str, Any]) -> None:
    # null или нет поля — контекст неизвестен: связь по одному testCaseId приписала бы
    # тесту чужую попытку и «прошёл после повтора».
    links = link_attempts([_result(1, hidden=True, testCaseId=7, **context),
                           _result(2, "passed", testCaseId=7, **context)])

    assert (links.linked_by, links.no_key, _linked(links)) == (None, 1, {})
    assert retry_warnings(links.info())


def test_explicitly_empty_context_is_a_link_key() -> None:
    context = {"testCaseId": 7, "parameters": [], "environment": {}}
    links = link_attempts([_result(1, hidden=True, **context), _result(2, **context)])

    assert (links.linked_by, _linked(links)) == (CONTEXT_KEY, {2: [1]})


def test_parameter_order_and_excluded_parameters_do_not_matter() -> None:
    attempt = _result(1, hidden=True, testCaseId=7, environment=[], parameters=[
        {"name": "b", "value": 2}, {"name": "a", "value": 1},
        {"name": "run", "value": "x-1", "excluded": True}])
    final = _result(2, testCaseId=7, environment=[], parameters=[
        {"name": "a", "value": "1"}, {"name": "b", "value": "2"},
        {"name": "run", "value": "x-2", "excluded": True}])

    assert _linked(link_attempts([attempt, final])) == {2: [1]}


def test_test_case_id_alone_is_not_a_link_key() -> None:
    links = link_attempts([_result(1, hidden=True, testCaseId=7), _result(2, testCaseId=7)])

    assert links.linked_by is None
    assert (links.hidden_total, links.no_key, _linked(links)) == (1, 1, {})


def test_several_finals_with_one_key_are_not_guessed() -> None:
    links = link_attempts([_result(1, hidden=True, historyId="h"), _result(2, historyId="h"),
                           _result(3, historyId="h")])

    assert (_linked(links), links.ambiguous) == ({}, 1)


@pytest.mark.parametrize("value", [None, "", "  ", True, {"id": 1}, ["h"]])
def test_odd_key_values_do_not_break_linking(value: object) -> None:
    links = link_attempts([_result(1, hidden=True, historyId="h"),
                           _result(2, hidden=True, historyId=value), _result(3, historyId="h")])

    assert _linked(links) == {3: [1]}
    assert links.no_key == 1


def test_no_hidden_results_means_no_link_field() -> None:
    assert link_attempts([_result(1, historyId="h")]).info() == RetryInfo()


def test_warnings_only_when_something_is_unlinked_or_unknown() -> None:
    assert retry_warnings(RetryInfo(linked_by="historyId", hidden_total=3, linked=3)) == []
    assert retry_warnings(RetryInfo(hidden_total=2, no_key=2)) == [
        ("Повторы не связаны с финальными результатами (скрытых попыток: 2): в ответах "
         "TestOps нет ни historyId/historyKey, ни testCaseId с параметрами и окружением.")
    ]
    assert retry_warnings(RetryInfo(linked_by="historyId", hidden_total=5, linked=3,
                                    no_final=1, ambiguous=1)) == [
        ("Не удалось связать 2 из 5 скрытых попыток (связь по historyId; без финального "
         "результата: 1, ключ у нескольких результатов: 1).")
    ]


class _Client:
    def __init__(self, results: list[ResultResponse],
                 details: dict[int, ResultResponse | Exception] | None = None) -> None:
        self.results = results
        self.details = details or {}
        self.detail_calls: list[int] = []

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        return LaunchResponse.model_validate({"id": launch_id, "name": "Launch"})

    async def get_all_test_results_for_launch(self, launch_id: int) -> list[ResultResponse]:
        return self.results

    async def get_test_result_execution(self, test_result_id: int) -> list[Any]:
        return []

    async def get_test_result_detail(self, test_result_id: int) -> ResultResponse:
        self.detail_calls.append(test_result_id)
        detail = self.details.get(test_result_id, ResultResponse(id=test_result_id))
        if isinstance(detail, Exception):
            raise detail
        return detail


def _settings(**overrides: Any) -> Settings:
    return Settings(endpoint="https://allure.test", token="t", **overrides)


@pytest.mark.asyncio
async def test_triage_counts_stay_without_hidden_and_report_links() -> None:
    client = _Client([
        _result(1, hidden=True, historyId="h", statusDetails={"message": "boom"}),
        _result(2, historyId="h", statusDetails={"message": "boom"}),
        _result(3, "passed"),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    assert (report.total_results, report.failed_count, report.passed_count) == (2, 1, 1)
    assert [test.test_result_id for test in report.failed_tests] == [2]
    assert report.retries.linked_by == "historyId"
    assert (report.retries.hidden_total, report.retries.linked) == (1, 1)


@pytest.mark.asyncio
async def test_triage_without_link_fields_works_as_before() -> None:
    client = _Client([_result(1, hidden=True, statusDetails={"message": "retry"}),
                      _result(2, statusDetails={"message": "boom"})])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    assert [test.test_result_id for test in report.failed_tests] == [2]
    assert report.failed_tests[0].attempts == []
    assert (report.retries.linked_by, report.retries.no_key) == (None, 1)


def _details(message: str) -> dict[str, Any]:
    return {"statusDetails": {"message": message}}


@pytest.mark.asyncio
async def test_attempt_errors_from_list_need_no_requests() -> None:
    client = _Client([
        _result(1, hidden=True, historyId="h", **_details("Total 0 at 2026-10-03 10:00:00")),
        _result(2, "broken", hidden=True, historyId="h",
                **_details("ConnectException: Connection refused")),
        _result(3, historyId="h", **_details("Total 0 at 2026-10-03 11:30:00")),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    attempts = report.failed_tests[0].attempts
    assert [(a.test_result_id, a.status.value, a.same_as_final) for a in attempts] == [
        (1, "failed", True), (2, "broken", False)]
    assert attempts[1].message == "ConnectException: Connection refused"
    assert client.detail_calls == []
    assert (report.retries.errors_total, report.retries.errors_known) == (2, 2)


@pytest.mark.asyncio
async def test_error_codes_make_attempt_errors_different() -> None:
    client = _Client([
        _result(1, hidden=True, historyId="h", **_details("Gateway error_code=10001")),
        _result(2, historyId="h", **_details("Gateway error_code=10002")),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    assert report.failed_tests[0].attempts[0].same_as_final is False


@pytest.mark.asyncio
async def test_thread_name_does_not_make_attempt_errors_different() -> None:
    client = _Client([
        _result(1, hidden=True, historyId="h",
                **_details("[http-nio-8080-exec-1] Cart total expected 300 but was 0")),
        _result(2, historyId="h", **_details("[http-nio-8080-exec-7] Cart total expected 300 but was 0")),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    assert report.failed_tests[0].attempts[0].same_as_final is True


@pytest.mark.asyncio
async def test_errors_are_compared_before_clipping() -> None:
    prefix = "Gateway rejected the request " + "x" * 400
    client = _Client([
        _result(1, hidden=True, historyId="h", **_details(prefix + " error_code=10001")),
        _result(2, hidden=True, historyId="h", **_details(prefix + " error_code=10002")),
        _result(3, historyId="h", **_details(prefix + " error_code=10002")),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    attempts = report.failed_tests[0].attempts
    assert attempts[0].message == attempts[1].message  # показ обрезан одинаково
    assert [a.same_as_final for a in attempts] == [False, True]


@pytest.mark.asyncio
async def test_attempt_without_list_error_is_fetched_and_failures_stay_unknown() -> None:
    client = _Client(
        [_result(1, hidden=True, historyId="h"), _result(2, hidden=True, historyId="h"),
         _result(3, historyId="h", **_details("boom"))],
        details={1: ResultResponse(id=1, trace="java.lang.IllegalStateException: boom\n\tat x"),
                 2: RuntimeError("503")},
    )

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    attempts = report.failed_tests[0].attempts
    assert sorted(client.detail_calls) == [1, 2]
    assert attempts[0].message == "java.lang.IllegalStateException: boom"
    assert attempts[0].same_as_final is False
    assert (attempts[1].message, attempts[1].same_as_final) == (None, None)
    assert (report.retries.errors_total, report.retries.errors_known) == (2, 1)


@pytest.mark.asyncio
async def test_only_last_five_attempts_and_request_cap() -> None:
    hidden = [_result(index, hidden=True, historyId="h") for index in range(1, 8)]
    client = _Client([*hidden, _result(8, historyId="h", **_details("boom"))])

    report = await TriageService(  # type: ignore[arg-type]
        client, _settings(retry_max_detail_requests=2)).analyze_launch(9)

    test = report.failed_tests[0]
    assert [a.test_result_id for a in test.attempts] == [3, 4, 5, 6, 7]
    assert test.attempts_omitted == 2
    assert client.detail_calls == [3, 4]
    assert (report.retries.errors_total, report.retries.errors_capped) == (5, 3)
    assert retry_warnings(report.retries) == [
        ("Ошибки повторов известны для 0 из 5 неудачных попыток; 3 не запрошены из-за лимита "
         "ALLURE_RETRY_MAX_DETAIL_REQUESTS.")
    ]


@pytest.mark.asyncio
async def test_passed_after_retry_is_listed_without_requests() -> None:
    client = _Client([
        _result(1, hidden=True, historyId="h", **_details("expected: <3> but was: <2>")),
        _result(2, hidden=True, historyId="h"),
        _result(3, "passed", historyId="h", fullName="ru.CartTest.addItem"),
        _result(4, "passed", historyId="other"),
    ])

    report = await TriageService(client, _settings()).analyze_launch(9)  # type: ignore[arg-type]

    assert client.detail_calls == []
    assert [item.model_dump() for item in report.retries.passed_after_retry] == [{
        "test_result_id": 3, "name": "test3", "full_name": "ru.CartTest.addItem",
        "link": "https://allure.test/launch/9/testresult/3", "failed_attempts": 2,
        "message": "expected: <3> but was: <2>",
    }]
    assert report.failed_tests == []


def test_long_attempt_message_is_clipped_to_first_line() -> None:
    from alla_core.services.triage_service import ATTEMPT_MESSAGE_CHARS, _attempt_message

    message = _attempt_message({"message": "\n  " + "x" * 400 + "\nsecond"}, None)

    assert message is not None and len(message) == ATTEMPT_MESSAGE_CHARS
    assert message.endswith("…")
