"""Абстрактный интерфейс для источников данных о результатах тестов."""

from typing import Protocol, runtime_checkable

from alla_skill.models.common import PageResponse
from alla_skill.models.testops import (
    AttachmentMeta,
    ExecutionStep,
    LaunchResponse,
    TestResultResponse,
)


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
        self,
        test_result_id: int,
    ) -> TestResultResponse:
        """Получить детальный результат теста по ID (GET /api/testresult/{id})."""
        ...

    async def get_test_result_execution(
        self,
        test_result_id: int,
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
        self,
        launch_id: int,
    ) -> list[TestResultResponse]:
        """Получить ВСЕ результаты тестов для запуска с обработкой пагинации."""
        ...


@runtime_checkable
class AttachmentProvider(Protocol):
    """Протокол для получения аттачментов результатов тестов.

    Разделён от TestResultsProvider для backward-compatibility:
    источники данных, не поддерживающие аттачменты, не обязаны реализовывать
    этот протокол. Проверка через ``isinstance(client, AttachmentProvider)``.

    Реализации:
    - AllureTestOpsClient: GET /api/testresult/attachment?testResultId={id}
                           GET /api/testresult/attachment/{id}/content
    """

    async def get_attachments_for_test_result(
        self,
        test_result_id: int,
    ) -> list[AttachmentMeta]:
        """Получить список аттачментов для результата теста."""
        ...

    async def get_attachment_content(
        self,
        attachment_id: int,
    ) -> bytes:
        """Скачать бинарное содержимое аттачмента."""
        ...
