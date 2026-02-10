"""Тесты Pydantic-моделей: алиасы, extra-поля, generic-парсинг, computed property."""

from __future__ import annotations

from alla.models.common import PageResponse
from alla.models.testops import TestResultResponse as ResultResponse
from alla.models.testops import TriageReport


def test_test_result_response_parses_camel_case_aliases() -> None:
    """camelCase-алиасы из API корректно маппятся в snake_case поля."""
    data = {
        "id": 1,
        "fullName": "com.example.LoginTest",
        "statusDetails": {"message": "assertion failed"},
        "testCaseId": 42,
        "launchId": 100,
    }
    result = ResultResponse.model_validate(data)

    assert result.id == 1
    assert result.full_name == "com.example.LoginTest"
    assert result.status_details == {"message": "assertion failed"}
    assert result.test_case_id == 42
    assert result.launch_id == 100


def test_test_result_response_accepts_extra_fields() -> None:
    """Неизвестные поля принимаются без ошибок (extra='allow')."""
    data = {"id": 1, "unknownField": "value", "anotherExtra": 123}
    result = ResultResponse.model_validate(data)

    assert result.id == 1


def test_page_response_parses_generic() -> None:
    """PageResponse[ResultResponse] корректно парсит content и метаданные."""
    data = {
        "content": [{"id": 1}, {"id": 2}],
        "totalElements": 2,
        "totalPages": 1,
        "size": 100,
        "number": 0,
    }
    page = PageResponse[ResultResponse].model_validate(data)

    assert len(page.content) == 2
    assert page.content[0].id == 1
    assert page.content[1].id == 2
    assert page.total_elements == 2
    assert page.total_pages == 1


def test_triage_report_failure_count_property() -> None:
    """failure_count — сумма failed_count и broken_count."""
    report = TriageReport(
        launch_id=1,
        total_results=10,
        passed_count=5,
        failed_count=3,
        broken_count=2,
        skipped_count=0,
        unknown_count=0,
        failed_tests=[],
    )

    assert report.failure_count == 5
