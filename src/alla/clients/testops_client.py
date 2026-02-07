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

    async def get_test_result_detail(
        self, test_result_id: int,
    ) -> TestResultResponse:
        """Получить детальный результат теста по ID.

        ``GET /api/testresult/{id}``

        Используется как fallback для получения top-level ``trace``, когда
        execution steps и statusDetails из пагинированного списка не содержат
        информации об ошибке.
        """
        data = await self._request(
            "GET", f"{self.TESTRESULT_ENDPOINT}/{test_result_id}",
        )
        return TestResultResponse.model_validate(data)

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
            "Ответ выполнения для результата теста %d: тип=%s, данные=%s",
            test_result_id,
            type(data).__name__,
            str(data)[:2000],
        )
        # Ответ — JSON-массив шагов
        if isinstance(data, list):
            return [ExecutionStep.model_validate(step) for step in data]
        # API может вернуть объект-обёртку {"steps": [...], "statusDetails": {...}, ...}
        # Wrapper парсится в ExecutionStep, сохраняя и свои данные об ошибке,
        # и вложенные steps. _find_failure_in_steps обойдёт дерево рекурсивно.
        if isinstance(data, dict):
            wrapper = ExecutionStep.model_validate(data)
            if wrapper.status_details or wrapper.message or wrapper.trace or wrapper.steps:
                return [wrapper]
            # Fallback: попытка извлечь шаги из ключа "content"
            content_raw = data.get("content")
            if isinstance(content_raw, list):
                return [ExecutionStep.model_validate(step) for step in content_raw]
            return [wrapper]
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
                "Получена страница %d/%d (пока собрано %d результатов)",
                page + 1,
                page_resp.total_pages,
                len(all_results),
            )

            if page + 1 >= page_resp.total_pages:
                break

            page += 1
            if page >= self._max_pages:
                logger.warning(
                    "Достигнут лимит max_pages (%d). Получено %d/%d результатов.",
                    self._max_pages,
                    len(all_results),
                    page_resp.total_elements,
                )
                raise PaginationLimitError(
                    f"Превышен max_pages={self._max_pages}. "
                    f"Получено {len(all_results)}/{page_resp.total_elements} "
                    f"результатов. Увеличьте ALLURE_MAX_PAGES при необходимости."
                )

        logger.info(
            "Получено %d результатов тестов для запуска %d (%d страниц)",
            len(all_results),
            launch_id,
            page + 1,
        )
        return all_results

    # --- Запись данных (протокол TestResultsUpdater) ---

    async def update_test_result_description(
        self,
        test_result_id: int,
        description: str,
        *,
        name: str,
    ) -> None:
        """Обновить description результата теста.

        ``PATCH /api/testresult/{id}``

        Allure TestOps требует обязательное поле ``name`` в теле PATCH-запроса,
        иначе возвращает 409 Validation Error.

        Raises:
            AllureApiError: При HTTP-ошибках.
        """
        await self._request(
            "PATCH",
            f"{self.TESTRESULT_ENDPOINT}/{test_result_id}",
            json={"name": name, "description": description},
            expect_json=False,
        )
        logger.debug(
            "Обновлён description для результата теста %d (%d символов)",
            test_result_id,
            len(description),
        )

    # --- Внутренний HTTP ---

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Выполнить аутентифицированный HTTP-запрос с повтором при 401.

        Возвращает десериализованный JSON (dict или list в зависимости от эндпоинта).
        Если ``expect_json=False``, пустое тело ответа возвращается как ``None``
        (для write-операций, которые могут вернуть 204 No Content).

        Raises:
            AllureApiError: При HTTP-ошибках в ответе.
            AuthenticationError: Если повторная аутентификация тоже не удалась.
        """
        url = f"{self._endpoint}{path}"
        auth_header = await self._auth.get_auth_header()

        logger.debug("HTTP-запрос: %s %s параметры=%s json=%s", method, url, params, json)

        try:
            resp = await self._http.request(
                method, url, params=params, json=json, headers=auth_header,
            )
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc

        # Одноразовый повтор при 401 (истёкший JWT)
        if resp.status_code == 401:
            logger.debug("Получен 401, выполняем повторную аутентификацию и повтор запроса")
            self._auth.invalidate()
            auth_header = await self._auth.get_auth_header()
            try:
                resp = await self._http.request(
                    method, url, params=params, json=json, headers=auth_header,
                )
            except httpx.RequestError as exc:
                raise AllureApiError(0, str(exc), path) from exc

        if resp.status_code == 404:
            raise AllureApiError(
                404,
                f"Эндпоинт не найден. Проверьте версию Allure TestOps. "
                f"Swagger UI: {self._endpoint}/swagger-ui.html",
                path,
            )

        if resp.status_code >= 400:
            body_text = resp.text[:500]
            raise AllureApiError(resp.status_code, body_text, path)

        if not resp.content:
            if not expect_json:
                return None
            raise AllureApiError(
                resp.status_code,
                "Ответ не содержит тела (пустой content)",
                path,
            )

        try:
            return resp.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise AllureApiError(
                resp.status_code,
                f"Ответ не является валидным JSON: {resp.text[:200]}",
                path,
            ) from exc

    # --- Жизненный цикл ---

    async def close(self) -> None:
        """Освободить ресурсы HTTP-клиента и менеджера аутентификации."""
        await self._http.aclose()
        await self._auth.close()

    async def __aenter__(self) -> AllureTestOpsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
