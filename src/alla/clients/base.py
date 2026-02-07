"""Абстрактный интерфейс для источников данных о результатах тестов."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.models.common import PageResponse
from alla.models.testops import ExecutionStep, LaunchResponse, TestResultResponse


@runtime_checkable
class TestResultsProvider(Protocol):
    """Протокол, определяющий контракт любого источника данных о результатах тестов.

    Реализации:
    - AllureTestOpsClient (MVP): получает данные из HTTP API Allure TestOps
    - Будущее: LocalAllureReportClient (читает JSON-файлы allure-report)
    - Будущее: CachedTestResultsClient (читает из локальной БД/кэша)
    """

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Получить метаданные запуска по ID."""
        ...

    async def get_test_result_detail(
        self, test_result_id: int,
    ) -> TestResultResponse:
        """Получить детальный результат теста по ID (GET /api/testresult/{id})."""
        ...

    async def get_test_result_execution(
        self, test_result_id: int,
    ) -> list[ExecutionStep]:
        """Получить дерево шагов выполнения теста по ID результата."""
        ...

    async def get_test_results_for_launch(
        self,
        launch_id: int,
        page: int = 0,
        size: int = 100,
    ) -> PageResponse[TestResultResponse]:
        """Получить одну страницу результатов тестов для заданного запуска."""
        ...

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Получить ВСЕ результаты тестов для запуска с обработкой пагинации."""
        ...


@runtime_checkable
class TestResultsUpdater(Protocol):
    """Протокол для записи данных обратно в источник результатов тестов.

    Разделён от TestResultsProvider для сохранения read/write separation.

    Реализации:
    - AllureTestOpsClient (MVP): PATCH через HTTP API Allure TestOps
    """

    async def update_test_result_description(
        self,
        test_result_id: int,
        description: str,
    ) -> None:
        """Обновить поле description у результата теста."""
        ...
