"""Pydantic-модели для ответов Allure TestOps API и доменных объектов."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from alla.models.common import TestStatus


class TestResultResponse(BaseModel):
    """Сырой результат теста из Allure TestOps API.

    Поля намеренно Optional там, где API может их не вернуть,
    что делает модель устойчивой к вариациям API. ``extra="allow"``
    захватывает любые недокументированные поля без ошибок валидации.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    full_name: str | None = Field(None, alias="fullName")
    status: str | None = None
    status_details: dict | None = Field(None, alias="statusDetails")
    trace: str | None = None
    duration: int | None = None
    test_case_id: int | None = Field(None, alias="testCaseId")
    test_case_name: str | None = Field(None, alias="testCaseName")
    launch_id: int | None = Field(None, alias="launchId")
    created_date: int | None = Field(None, alias="createdDate")
    category: str | None = None
    muted: bool = False
    hidden: bool = False


class LaunchResponse(BaseModel):
    """Метаданные запуска из Allure TestOps API."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    closed: bool = False
    created_date: int | None = Field(None, alias="createdDate")


class ExecutionStep(BaseModel):
    """Шаг выполнения теста из ``/api/testresult/{id}/execution``.

    Ответ эндпоинта — дерево шагов. Каждый шаг может содержать вложенные
    ``steps``, а также ``statusDetails`` с сообщением об ошибке и стек-трейсом.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str | None = None
    status: str | None = None
    status_details: dict | None = Field(None, alias="statusDetails")
    message: str | None = None
    trace: str | None = None
    steps: list[ExecutionStep] | None = None
    duration: int | None = None
    parameters: list[dict] | None = None
    attachments: list[dict] | None = None


class FailedTestSummary(BaseModel):
    """Доменная модель: краткое описание упавшего теста для вывода триажа."""

    test_result_id: int
    name: str
    full_name: str | None = None
    status: TestStatus
    category: str | None = None
    status_message: str | None = None
    status_trace: str | None = None
    execution_steps: list[ExecutionStep] | None = None
    test_case_id: int | None = None
    link: str | None = None
    duration_ms: int | None = None


class TriageReport(BaseModel):
    """Результат шага триажа: сводка падений запуска."""

    launch_id: int
    launch_name: str | None = None
    total_results: int
    passed_count: int = 0
    failed_count: int = 0
    broken_count: int = 0
    skipped_count: int = 0
    unknown_count: int = 0
    failed_tests: list[FailedTestSummary] = []

    @property
    def failure_count(self) -> int:
        return self.failed_count + self.broken_count
