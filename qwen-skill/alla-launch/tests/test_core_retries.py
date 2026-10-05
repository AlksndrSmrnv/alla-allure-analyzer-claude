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


def test_parameter_order_and_excluded_parameters_do_not_matter() -> None:
    attempt = _result(1, hidden=True, testCaseId=7, parameters=[
        {"name": "b", "value": 2}, {"name": "a", "value": 1},
        {"name": "run", "value": "x-1", "excluded": True}])
    final = _result(2, testCaseId=7, parameters=[
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
        "Повторы не связаны с финальными результатами (скрытых попыток: 2): в ответах "
        "TestOps нет ни historyId/historyKey, ни testCaseId с параметрами или окружением."
    ]
    assert retry_warnings(RetryInfo(linked_by="historyId", hidden_total=5, linked=3,
                                    no_final=1, ambiguous=1)) == [
        "Не удалось связать 2 из 5 скрытых попыток (связь по historyId; без финального "
        "результата: 1, ключ у нескольких результатов: 1)."
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
