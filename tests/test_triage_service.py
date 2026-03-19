"""Behavioral tests for TriageService."""

from __future__ import annotations

import pytest

from alla.config import Settings
from alla.models.testops import LaunchResponse as LaunchModel, TestResultResponse as ResultResponse
from alla.services.triage_service import TriageService
from conftest import make_execution_step


def _make_settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.test")
    monkeypatch.setenv("ALLURE_TOKEN", "test-token")
    return Settings()


def _make_failed_result(**overrides) -> ResultResponse:
    payload: dict[str, object] = {
        "id": 100,
        "name": "test_login",
        "status": "failed",
    }
    payload.update(overrides)
    return ResultResponse.model_validate(payload)


class _Client:
    def __init__(
        self,
        *,
        results: list[ResultResponse],
        execution_by_id: dict[int, list] | None = None,
        detail_by_id: dict[int, ResultResponse | Exception] | None = None,
    ) -> None:
        self._results = results
        self._execution_by_id = execution_by_id or {}
        self._detail_by_id = detail_by_id or {}
        self.detail_calls = 0

    async def get_launch(self, launch_id: int) -> LaunchModel:
        return LaunchModel.model_validate(
            {"id": launch_id, "name": "Launch", "projectId": 42},
        )

    async def get_all_test_results_for_launch(
        self,
        launch_id: int,
    ) -> list[ResultResponse]:
        return self._results

    async def get_test_result_execution(self, test_result_id: int) -> list:
        return self._execution_by_id.get(test_result_id, [])

    async def get_test_result_detail(self, test_result_id: int) -> ResultResponse:
        self.detail_calls += 1
        detail = self._detail_by_id[test_result_id]
        if isinstance(detail, Exception):
            raise detail
        return detail


@pytest.mark.asyncio
async def test_analyze_launch_skips_detail_fetch_when_error_already_present(
    monkeypatch,
    tmp_path,
) -> None:
    """Fallback detail fetch is skipped when execution already contains the error."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=1)
    client = _Client(
        results=[result],
        execution_by_id={
            1: [
                make_execution_step(
                    status="failed",
                    message="from execution",
                    trace="stack line",
                )
            ]
        },
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert client.detail_calls == 0
    assert report.failed_tests[0].status_message == "from execution"
    assert report.failed_tests[0].status_trace == "stack line"


@pytest.mark.asyncio
async def test_analyze_launch_fills_trace_from_detail_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    """Missing execution/statusDetails are backfilled from GET /api/testresult/{id}."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=2)
    detail = ResultResponse.model_validate(
        {
            "id": 2,
            "trace": "java.lang.NullPointerException\n\tat Test.run(Test.java:42)",
        }
    )
    client = _Client(
        results=[result],
        detail_by_id={2: detail},
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert client.detail_calls == 1
    assert report.failed_tests[0].status_message == "java.lang.NullPointerException"
    assert "Test.run" in (report.failed_tests[0].status_trace or "")


@pytest.mark.asyncio
async def test_analyze_launch_ignores_detail_fetch_error(
    monkeypatch,
    tmp_path,
) -> None:
    """Detail fallback errors do not abort triage."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=3)
    client = _Client(
        results=[result],
        detail_by_id={3: RuntimeError("detail unavailable")},
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert client.detail_calls == 1
    assert report.failed_tests[0].status_message is None
    assert report.failed_tests[0].status_trace is None
