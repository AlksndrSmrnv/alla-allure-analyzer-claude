"""Абстрактный интерфейс для источников данных о результатах тестов."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alla.models.common import PageResponse
from alla.models.testops import CommentResponse, ExecutionStep, LaunchResponse, TestResultResponse


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
    ) -> list:
        """Получить список аттачментов для результата теста.

        Args:
            test_result_id: ID результата теста.

        Returns:
            Список AttachmentMeta с метаданными аттачментов.
        """
        ...

    async def get_attachment_content(
        self,
        attachment_id: int,
    ) -> bytes:
        """Скачать бинарное содержимое аттачмента.

        Args:
            attachment_id: ID аттачмента (поле ``id`` из AttachmentMeta).

        Returns:
            Бинарное содержимое файла аттачмента.
        """
        ...


@runtime_checkable
class TestResultsUpdater(Protocol):
    """Протокол для записи данных обратно в источник результатов тестов.

    Разделён от TestResultsProvider для сохранения read/write separation.

    Реализации:
    - AllureTestOpsClient (MVP): POST /api/comment через HTTP API Allure TestOps
    """

    async def post_comment(
        self,
        test_case_id: int,
        body: str,
    ) -> None:
        """Добавить комментарий к тест-кейсу.

        Args:
            test_case_id: ID тест-кейса (не test_result_id).
            body: Текст комментария.
        """
        ...


@runtime_checkable
class CommentManager(Protocol):
    """Протокол для чтения и удаления комментариев к тест-кейсам.

    Разделён от TestResultsUpdater для backward-compatibility:
    источники данных, не поддерживающие управление комментариями,
    не обязаны реализовывать этот протокол.
    Проверка через ``isinstance(client, CommentManager)``.

    Реализации:
    - AllureTestOpsClient: GET/DELETE /api/comment
    """

    async def get_comments(self, test_case_id: int) -> list[CommentResponse]:
        """Получить все комментарии для тест-кейса.

        Args:
            test_case_id: ID тест-кейса.

        Returns:
            Список комментариев.
        """
        ...

    async def delete_comment(self, comment_id: int) -> None:
        """Удалить комментарий по ID.

        Args:
            comment_id: ID комментария.
        """
        ...


@runtime_checkable
class LaunchLinksUpdater(Protocol):
    """Протокол для обновления ссылок запуска через PATCH /api/launch/{id}.

    Разделён от TestResultsUpdater: не все источники данных поддерживают
    обновление метаданных запуска. Проверка через
    ``isinstance(client, LaunchLinksUpdater)``.

    Реализации:
    - AllureTestOpsClient: GET /api/launch/{id} → merge links → PATCH /api/launch/{id}
    """

    async def patch_launch_links(self, launch_id: int, name: str, url: str) -> None:
        """Добавить ссылку к массиву links запуска.

        Алгоритм: GET текущий JSON запуска → добавить ссылку → PATCH.

        Args:
            launch_id: ID запуска в Allure TestOps.
            name: Отображаемое имя ссылки.
            url: URL ссылки.
        """
        ...
