"""Тесты бизнес-логики TriageService: нормализация статусов, извлечение ошибок, сборка сводки."""

from __future__ import annotations

import pytest

from alla.config import Settings
from alla.models.common import TestStatus
from alla.models.testops import ExecutionStep, TestResultResponse
from alla.services.triage_service import TriageService
from conftest import make_execution_step


# ---------------------------------------------------------------------------
# _normalize_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("passed", TestStatus.PASSED),
        ("failed", TestStatus.FAILED),
        ("broken", TestStatus.BROKEN),
        ("skipped", TestStatus.SKIPPED),
    ],
)
def test_normalize_status_maps_standard_values(raw: str, expected: TestStatus) -> None:
    """Стандартные строки статусов маппятся в соответствующий TestStatus."""
    assert TriageService._normalize_status(raw) is expected


def test_normalize_status_none_returns_unknown() -> None:
    """None-статус трактуется как UNKNOWN."""
    assert TriageService._normalize_status(None) is TestStatus.UNKNOWN


def test_normalize_status_invalid_returns_unknown() -> None:
    """Неизвестная строка статуса трактуется как UNKNOWN."""
    assert TriageService._normalize_status("garbage") is TestStatus.UNKNOWN


def test_normalize_status_is_case_insensitive() -> None:
    """Нормализация регистронезависима: 'FAILED', 'Failed' → FAILED."""
    assert TriageService._normalize_status("FAILED") is TestStatus.FAILED
    assert TriageService._normalize_status("Failed") is TestStatus.FAILED


# ---------------------------------------------------------------------------
# _extract_error_from_step
# ---------------------------------------------------------------------------

def test_extract_error_direct_fields() -> None:
    """Извлечение ошибки из прямых полей message/trace шага."""
    step = make_execution_step(message="assertion failed", trace="at Test.java:10")
    msg, trace = TriageService._extract_error_from_step(step)

    assert msg == "assertion failed"
    assert trace == "at Test.java:10"


def test_extract_error_from_status_details_dict() -> None:
    """Извлечение ошибки из вложенного dict statusDetails."""
    step = make_execution_step(
        status_details={"message": "NPE", "trace": "at Service.java:42"},
    )
    msg, trace = TriageService._extract_error_from_step(step)

    assert msg == "NPE"
    assert trace == "at Service.java:42"


def test_extract_error_prefers_direct_over_status_details() -> None:
    """Прямые поля message/trace приоритетнее statusDetails."""
    step = make_execution_step(
        message="direct error",
        status_details={"message": "nested error"},
    )
    msg, trace = TriageService._extract_error_from_step(step)

    assert msg == "direct error"


# ---------------------------------------------------------------------------
# _find_failure_in_steps
# ---------------------------------------------------------------------------

def test_find_failure_returns_first_failed_step() -> None:
    """Из списка шагов возвращается ошибка первого шага со статусом failed/broken."""
    steps = [
        make_execution_step(status="passed"),
        make_execution_step(
            status="failed",
            message="expected 200 got 500",
            trace="at ApiTest.java:33",
        ),
    ]
    msg, trace = TriageService._find_failure_in_steps(steps)

    assert msg == "expected 200 got 500"
    assert trace == "at ApiTest.java:33"


def test_find_failure_recurses_into_nested_steps() -> None:
    """Рекурсивный поиск: ошибка во вложенном шаге извлекается корректно."""
    child = make_execution_step(
        status="failed",
        message="nested failure",
    )
    parent = ExecutionStep.model_validate({"status": "failed", "steps": [child.model_dump()]})
    steps = [parent]

    msg, trace = TriageService._find_failure_in_steps(steps)

    # Родитель имеет status=failed но без message, ребёнок — с message
    # Алгоритм рекурсивно найдёт ребёнка
    assert msg == "nested failure"


def test_find_failure_second_pass_for_no_status() -> None:
    """Второй проход: шаг без status, но с statusDetails — находит ошибку."""
    steps = [
        make_execution_step(status="passed"),
        make_execution_step(
            status_details={"message": "root error", "trace": "root stack"},
        ),
    ]
    msg, trace = TriageService._find_failure_in_steps(steps)

    assert msg == "root error"
    assert trace == "root stack"


# ---------------------------------------------------------------------------
# _build_failed_summary
# ---------------------------------------------------------------------------

def _make_triage_service(monkeypatch, tmp_path) -> TriageService:
    """Создать TriageService с минимальным Settings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.test")
    monkeypatch.setenv("ALLURE_TOKEN", "test-token")
    settings = Settings()

    class _DummyClient:
        pass

    return TriageService(_DummyClient(), settings)  # type: ignore[arg-type]


def test_build_summary_uses_execution_over_result(monkeypatch, tmp_path) -> None:
    """Ошибка из execution steps приоритетнее result.statusDetails."""
    service = _make_triage_service(monkeypatch, tmp_path)
    result = TestResultResponse.model_validate({
        "id": 1,
        "name": "test_login",
        "status": "failed",
        "statusDetails": {"message": "fallback msg"},
    })
    exec_steps = [
        make_execution_step(status="failed", message="from step"),
    ]

    summary = service._build_failed_summary(result, exec_steps, launch_id=10)

    assert summary.status_message == "from step"
    assert summary.link == "https://allure.test/launch/10/testresult/1"


def test_build_summary_fallback_to_result_status_details(monkeypatch, tmp_path) -> None:
    """При пустых execution steps — fallback на statusDetails результата."""
    service = _make_triage_service(monkeypatch, tmp_path)
    result = TestResultResponse.model_validate({
        "id": 2,
        "name": "test_payment",
        "status": "broken",
        "statusDetails": {"message": "timeout", "trace": "at Gateway.java:55"},
    })

    summary = service._build_failed_summary(result, [], launch_id=10)

    assert summary.status_message == "timeout"
    assert summary.status_trace == "at Gateway.java:55"
