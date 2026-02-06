"""Реализация HTTP-клиента Allure TestOps."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alla.clients.auth import AllureAuthManager
from alla.config import Settings
from alla.exceptions import AllureApiError, PaginationLimitError
from alla.models.common import PageResponse
from alla.models.testops import ExecutionStep, LaunchResponse, TestResultResponse

logger = logging.getLogger(__name__)


class AllureTestOpsClient:
    """HTTP-клиент для REST API Allure TestOps.

    Реализует протокол :class:`~alla.clients.base.TestResultsProvider`.

    Пути эндпоинтов — атрибуты класса, чтобы их можно было переопределить,
    если целевая версия Allure TestOps использует другую структуру API.
    """

    LAUNCH_ENDPOINT = "/api/launch"
    TESTRESULT_ENDPOINT = "/api/testresult"

    def __init__(self, settings: Settings, auth_manager: AllureAuthManager) -> None:
        self._endpoint = str(settings.endpoint).rstrip("/")
        self._auth = auth_manager
        self._page_size = settings.page_size
        self._max_pages = settings.max_pages
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout,
            verify=settings.ssl_verify,
        )

    # --- Публичный API (протокол TestResultsProvider) ---

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Получить метаданные запуска по ID.

        ``GET /api/launch/{id}``
        """
        data = await self._request("GET", f"{self.LAUNCH_ENDPOINT}/{launch_id}")
        return LaunchResponse.model_validate(data)

    async def get_test_result_execution(
        self, test_result_id: int,
    ) -> list[ExecutionStep]:
        """Получить дерево шагов выполнения теста.

        ``GET /api/testresult/{id}/execution``

        Возвращает список корневых шагов (execution steps) с вложенными
        ``steps``, ``statusDetails`` (сообщение об ошибке и стек-трейс),
        ``attachments`` и другими деталями исполнения.
        """
        data = await self._request(
            "GET", f"{self.TESTRESULT_ENDPOINT}/{test_result_id}/execution",
        )
        logger.debug(
            "Execution response for test_result %d: type=%s, data=%s",
            test_result_id,
            type(data).__name__,
            str(data)[:2000],
        )
        # Ответ — JSON-массив шагов
        if isinstance(data, list):
            return [ExecutionStep.model_validate(step) for step in data]
        # Если API вернул объект-обёртку — сам объект может содержать
        # statusDetails с ошибкой. Возвращаем его как корневой шаг,
        # чтобы _find_failure_in_steps мог найти ошибку на любом уровне.
        if isinstance(data, dict):
            return [ExecutionStep.model_validate(data)]
        return []

    async def get_test_results_for_launch(
        self,
        launch_id: int,
        page: int = 0,
        size: int | None = None,
    ) -> PageResponse[TestResultResponse]:
        """Получить одну страницу результатов тестов для заданного запуска.

        ``GET /api/testresult?launchId={id}&page={page}&size={size}``
        """
        params: dict[str, Any] = {
            "launchId": launch_id,
            "page": page,
            "size": size or self._page_size,
        }
        data = await self._request("GET", self.TESTRESULT_ENDPOINT, params=params)
        return PageResponse[TestResultResponse].model_validate(data)

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Получить ВСЕ результаты тестов для запуска, итерируя по страницам.

        Останавливается, когда все страницы получены или достигнут защитный
        лимит ``max_pages``.
        """
        all_results: list[TestResultResponse] = []
        page = 0

        while True:
            page_resp = await self.get_test_results_for_launch(
                launch_id, page=page, size=self._page_size,
            )
            all_results.extend(page_resp.content)

            logger.debug(
                "Fetched page %d/%d (%d results so far)",
                page + 1,
                page_resp.total_pages,
                len(all_results),
            )

            if page + 1 >= page_resp.total_pages:
                break

            page += 1
            if page >= self._max_pages:
                logger.warning(
                    "Reached max_pages limit (%d). %d/%d total results fetched.",
                    self._max_pages,
                    len(all_results),
                    page_resp.total_elements,
                )
                raise PaginationLimitError(
                    f"Exceeded max_pages={self._max_pages}. "
                    f"Fetched {len(all_results)}/{page_resp.total_elements} results. "
                    f"Increase ALLURE_MAX_PAGES if needed."
                )

        logger.info(
            "Fetched %d test results for launch %d (%d pages)",
            len(all_results),
            launch_id,
            page + 1,
        )
        return all_results

    # --- Внутренний HTTP ---

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Выполнить аутентифицированный HTTP-запрос с повтором при 401.

        Возвращает десериализованный JSON (dict или list в зависимости от эндпоинта).

        Raises:
            AllureApiError: При HTTP-ошибках в ответе.
            AuthenticationError: Если повторная аутентификация тоже не удалась.
        """
        url = f"{self._endpoint}{path}"
        auth_header = await self._auth.get_auth_header()

        logger.debug("%s %s params=%s", method, url, params)

        try:
            resp = await self._http.request(
                method, url, params=params, headers=auth_header,
            )
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc

        # Одноразовый повтор при 401 (истёкший JWT)
        if resp.status_code == 401:
            logger.debug("Got 401, re-authenticating and retrying")
            self._auth.invalidate()
            auth_header = await self._auth.get_auth_header()
            try:
                resp = await self._http.request(
                    method, url, params=params, headers=auth_header,
                )
            except httpx.RequestError as exc:
                raise AllureApiError(0, str(exc), path) from exc

        if resp.status_code == 404:
            raise AllureApiError(
                404,
                f"Endpoint not found. Check your Allure TestOps version. "
                f"Swagger UI: {self._endpoint}/swagger-ui.html",
                path,
            )

        if resp.status_code >= 400:
            body_text = resp.text[:500]
            raise AllureApiError(resp.status_code, body_text, path)

        return resp.json()  # type: ignore[no-any-return]

    # --- Жизненный цикл ---

    async def close(self) -> None:
        """Освободить ресурсы HTTP-клиента и менеджера аутентификации."""
        await self._http.aclose()
        await self._auth.close()

    async def __aenter__(self) -> AllureTestOpsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
