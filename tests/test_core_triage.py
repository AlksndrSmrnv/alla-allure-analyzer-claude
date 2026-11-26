"""Поведенческие тесты TriageService."""

from __future__ import annotations


import pytest

import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path
from alla_core.config import Settings
from alla_core.models.testops import LaunchResponse as LaunchModel, TestResultResponse as ResultResponse
from alla_core.services.triage_service import TriageService
from skill_factories import make_execution_step


def _make_settings(monkeypatch, tmp_path) -> Settings:
    # Settings скилла — dataclass: окружение читает только Settings.load,
    # а тесту нужны фиксированные значения.
    return Settings(endpoint="https://allure.test", token="test-token")


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
    """Fallback detail fetch пропускается, если execution уже содержит ошибку."""
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
    """Отсутствующие execution/statusDetails дополняются из GET /api/testresult/{id}."""
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
async def test_missing_trace_is_fetched_even_when_message_is_known(
    monkeypatch,
    tmp_path,
) -> None:
    """В списке только сообщение (общий ассерт), трейс с причиной — в GET /api/testresult/{id}."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=3, statusDetails={"message": "expected: <200> but was: <500>"})
    detail = ResultResponse.model_validate({
        "id": 3,
        "statusDetails": {"message": "other text"},
        "trace": "java.lang.AssertionError: expected: <200> but was: <500>\n"
                 "\tat ru.OrderTest.create(OrderTest.java:42)\n"
                 "Caused by: java.lang.NullPointerException",
    })
    client = _Client(results=[result], detail_by_id={3: detail})

    report = await TriageService(client, settings).analyze_launch(123)

    assert client.detail_calls == 1
    assert report.failed_tests[0].status_message == "expected: <200> but was: <500>"
    assert "NullPointerException" in (report.failed_tests[0].status_trace or "")


@pytest.mark.asyncio
async def test_step_path_reaches_deepest_but_message_stays_at_outer_failed_step(
    monkeypatch,
    tmp_path,
) -> None:
    """Step path спускается до глубокого failed-узла, а message — с внешнего failed-шага.

    У внешнего failed-шага обычно полный assertion-префикс, у вложенного —
    его обрезанный фрагмент. Берём message внешнего, чтобы «Пример ошибки» в
    отчёте содержал начало строки.
    """
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=10)
    client = _Client(
        results=[result],
        execution_by_id={
            10: [
                make_execution_step(
                    name="outer",
                    status="failed",
                    message="outer msg",
                    steps=[
                        make_execution_step(
                            name="inner",
                            status="failed",
                            message="inner msg",
                            steps=[
                                make_execution_step(name="leaf", status="passed"),
                            ],
                        ),
                    ],
                ),
            ],
        },
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert report.failed_tests[0].failed_step_path == "outer → inner"
    assert report.failed_tests[0].status_message == "outer msg"


@pytest.mark.asyncio
async def test_step_path_falls_back_to_outer_message_when_leaf_has_none(
    monkeypatch,
    tmp_path,
) -> None:
    """Если у самого глубокого failed-шага нет своего message — берём с failed-предка."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=11)
    client = _Client(
        results=[result],
        execution_by_id={
            11: [
                make_execution_step(
                    name="outer",
                    status="failed",
                    message="outer msg",
                    trace="outer trace",
                    steps=[
                        make_execution_step(name="inner", status="failed"),
                    ],
                ),
            ],
        },
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert report.failed_tests[0].failed_step_path == "outer → inner"
    assert report.failed_tests[0].status_message == "outer msg"
    assert report.failed_tests[0].status_trace == "outer trace"


@pytest.mark.asyncio
async def test_step_path_picks_up_statusless_wrapper_status_details(
    monkeypatch,
    tmp_path,
) -> None:
    """Statusless wrapper с statusDetails + вложенный failed без error — error берём с wrapper."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=12)
    client = _Client(
        results=[result],
        execution_by_id={
            12: [
                make_execution_step(
                    name="wrapper",
                    status_details={
                        "message": "wrapper msg",
                        "trace": "wrapper trace",
                    },
                    steps=[
                        make_execution_step(name="leaf", status="failed"),
                    ],
                ),
            ],
        },
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert report.failed_tests[0].failed_step_path == "wrapper → leaf"
    assert report.failed_tests[0].status_message == "wrapper msg"
    assert report.failed_tests[0].status_trace == "wrapper trace"


@pytest.mark.asyncio
async def test_step_path_fills_trace_from_wrapper_when_leaf_has_only_message(
    monkeypatch,
    tmp_path,
) -> None:
    """Wrapper-trace дозаполняется даже если у leaf уже есть свой message."""
    settings = _make_settings(monkeypatch, tmp_path)
    result = _make_failed_result(id=13)
    client = _Client(
        results=[result],
        execution_by_id={
            13: [
                make_execution_step(
                    name="wrapper",
                    status_details={
                        "message": "wrapper msg",
                        "trace": "wrapper trace",
                    },
                    steps=[
                        make_execution_step(
                            name="leaf",
                            status="failed",
                            message="leaf msg",
                        ),
                    ],
                ),
            ],
        },
    )

    report = await TriageService(client, settings).analyze_launch(123)

    assert report.failed_tests[0].failed_step_path == "wrapper → leaf"
    assert report.failed_tests[0].status_message == "leaf msg"
    assert report.failed_tests[0].status_trace == "wrapper trace"


@pytest.mark.asyncio
async def test_analyze_launch_ignores_detail_fetch_error(
    monkeypatch,
    tmp_path,
) -> None:
    """Ошибки detail fallback не прерывают triage."""
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


@pytest.mark.parametrize("value", [42, True, ["oops"], {"nested": "oops"}])
def test_status_details_reject_non_strings(monkeypatch, tmp_path, value):
    from alla_core.models.testops import ExecutionStep

    service = TriageService(_Client(results=[]), _make_settings(monkeypatch, tmp_path))
    step = ExecutionStep(
        name="step", status="failed", statusDetails={"message": value, "trace": value}
    )
    assert service._extract_error_from_step(step) == (None, None)
    summary = service._build_failed_summary(
        _make_failed_result(statusDetails={"message": value, "trace": value}), [step], 123
    )
    assert summary.status_message is None
    assert summary.status_trace is None
