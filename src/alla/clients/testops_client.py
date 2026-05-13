"""Реализация HTTP-клиента Allure TestOps."""

import logging
from typing import Any

import httpx

from alla.clients.auth import AllureAuthManager
from alla.config import Settings
from alla.exceptions import AllaError, AllureApiError, PaginationLimitError
from alla.models.common import PageResponse
from alla.models.testops import AttachmentMeta, CommentResponse, ExecutionStep, LaunchResponse, TestResultResponse

logger = logging.getLogger(__name__)


class AllureTestOpsClient:
    """HTTP-клиент для REST API Allure TestOps.

    Реализует протокол :class:`~alla.clients.base.TestResultsProvider`.

    Пути эндпоинтов — атрибуты класса, чтобы их можно было переопределить,
    если целевая версия Allure TestOps использует другую структуру API.
    """

    LAUNCH_ENDPOINT = "/api/launch"
    TESTRESULT_ENDPOINT = "/api/testresult"
    COMMENT_ENDPOINT = "/api/comment"
    ATTACHMENT_ENDPOINT = "/api/testresult/attachment"
    PROJECT_ENDPOINT = "/api/project"

    def __init__(self, settings: Settings, auth_manager: AllureAuthManager) -> None:
        self._endpoint = str(settings.endpoint).rstrip("/")
        self._auth = auth_manager
        self._page_size = settings.page_size
        self._max_pages = settings.max_pages
        self._max_attachment_bytes = settings.logs_max_attachment_bytes
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout,
            verify=settings.ssl_verify,
        )

    # --- Публичный API (протокол TestResultsProvider) ---

    async def find_launch_by_name(
        self,
        name: str,
        project_id: int | None = None,
    ) -> int:
        """Найти ID запуска по точному совпадению имени.

        ``GET /api/launch?projectId=X&page=0&size={page_size}&sort=created_date,DESC``

        Возвращает ID первого найденного запуска или бросает :class:`AllureApiError`.
        """
        params: dict[str, Any] = {"page": 0, "size": self._page_size, "sort": "created_date,DESC"}
        if project_id is not None:
            params["projectId"] = project_id

        logger.info("Поиск запуска по имени '%s' (projectId=%s)...", name, project_id)
        data = await self._request("GET", self.LAUNCH_ENDPOINT, params=params)
        content = data.get("content", []) if isinstance(data, dict) else []

        found_names = [lch.get("name") for lch in content if isinstance(lch, dict)]
        logger.debug("Последние %d запусков проекта: %s", len(found_names), found_names)

        for launch in content:
            if isinstance(launch, dict) and launch.get("name") == name:
                launch_id = int(launch["id"])
                logger.info("Найден запуск '%s' → ID %d", name, launch_id)
                return launch_id

        raise AllaError(
            f"Запуск '{name}' не найден в последних {len(content)} запусках проекта "
            f"(projectId={project_id}). Доступные имена: {found_names}"
        )

    async def list_projects(self) -> dict[int, str]:
        """Вернуть отображение ``{project_id: name}`` со всех страниц ``/api/project``.

        Постранично обходит ``GET /api/project?page=N&size={page_size}``,
        пока не закончатся страницы или не сработает защита ``max_pages``.
        Используется на сервере для получения человекочитаемых имён проектов.
        """
        out: dict[int, str] = {}
        page = 0
        while page < self._max_pages:
            params: dict[str, Any] = {"page": page, "size": self._page_size}
            data = await self._request("GET", self.PROJECT_ENDPOINT, params=params)
            content = data.get("content", []) if isinstance(data, dict) else []
            for project in content:
                if not isinstance(project, dict):
                    continue
                pid = project.get("id")
                name = project.get("name")
                if isinstance(pid, int) and isinstance(name, str):
                    out[pid] = name
            total_pages = data.get("totalPages") if isinstance(data, dict) else None
            page += 1
            if not isinstance(total_pages, int) or page >= total_pages:
                break
            if not content:
                break
        return out

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

    async def post_comment(
        self,
        test_case_id: int,
        body: str,
    ) -> None:
        """Добавить комментарий к тест-кейсу.

        ``POST /api/comment``
        """
        payload = {"testCaseId": test_case_id, "body": body}
        logger.debug(
            "POST comment для test_case %d: body=%s",
            test_case_id,
            body[:100] + "..." if len(body) > 100 else body,
        )
        result = await self._request(
            "POST",
            self.COMMENT_ENDPOINT,
            json=payload,
            expect_json=False,
        )
        logger.debug(
            "POST comment для test_case %d: ответ=%s",
            test_case_id,
            str(result)[:500] if result is not None else "None (пустое тело)",
        )

    async def patch_launch_links(self, launch_id: int, name: str, url: str) -> None:
        """Добавить ссылку к запуску.

        Алгоритм:
        1. ``GET /api/launch/{id}`` — получить текущий JSON запуска.
        2. Добавить новую ссылку к существующему массиву ``links``.
        3. ``PATCH /api/launch/{id}`` — отправить обновлённый JSON.
        """
        data = await self._request("GET", f"{self.LAUNCH_ENDPOINT}/{launch_id}")
        if not isinstance(data, dict):
            raise AllureApiError(
                0,
                "Неожиданный формат ответа GET /api/launch/{id}",
                f"{self.LAUNCH_ENDPOINT}/{launch_id}",
            )

        existing_links = data.get("links") or []
        if not isinstance(existing_links, list):
            existing_links = []

        patch_payload = dict(data)
        patch_payload["links"] = existing_links + [{"name": name, "url": url}]

        logger.info(
            "PATCH /api/launch/%d: добавление ссылки '%s' → '%s'",
            launch_id, name, url,
        )
        await self._request(
            "PATCH",
            f"{self.LAUNCH_ENDPOINT}/{launch_id}",
            json=patch_payload,
            expect_json=False,
        )
        logger.debug("Ссылка добавлена к запуску #%d", launch_id)

    # --- Комментарии (протокол CommentManager) ---

    async def get_comments(self, test_case_id: int) -> list[CommentResponse]:
        """Получить все комментарии для тест-кейса.

        ``GET /api/comment?testCaseId={id}&size=1000``
        """
        params = {
            "testCaseId": test_case_id,
            "size": 1000,
        }
        logger.debug("Получение комментариев для тест-кейса %d", test_case_id)
        data = await self._request("GET", self.COMMENT_ENDPOINT, params=params)

        content = data.get("content", []) if isinstance(data, dict) else []
        comments = [CommentResponse.model_validate(item) for item in content]

        logger.debug(
            "Получено %d комментариев для тест-кейса %d",
            len(comments),
            test_case_id,
        )
        return comments

    async def delete_comment(self, comment_id: int) -> None:
        """Удалить комментарий по ID.

        ``DELETE /api/comment/{id}``
        """
        logger.debug("Удаление комментария %d", comment_id)
        await self._request(
            "DELETE",
            f"{self.COMMENT_ENDPOINT}/{comment_id}",
            expect_json=False,
        )
        logger.debug("Комментарий %d удалён", comment_id)

    # --- Аттачменты (протокол AttachmentProvider) ---

    async def get_attachments_for_test_result(
        self,
        test_result_id: int,
    ) -> list[AttachmentMeta]:
        """Получить список аттачментов для результата теста.

        ``GET /api/testresult/attachment?testResultId={id}&size=1000``
        """
        params = {
            "testResultId": test_result_id,
            "size": 1000,  # Достаточно большой размер для получения всех аттачментов
        }
        logger.debug(
            "Получение списка аттачментов для результата теста %d",
            test_result_id,
        )
        data = await self._request("GET", self.ATTACHMENT_ENDPOINT, params=params)

        content = data.get("content", []) if isinstance(data, dict) else []
        attachments = [AttachmentMeta.model_validate(item) for item in content]

        logger.debug(
            "Получено %d аттачментов для результата теста %d",
            len(attachments),
            test_result_id,
        )
        return attachments

    async def get_attachment_content(
        self,
        attachment_id: int,
    ) -> bytes:
        """Скачать бинарное содержимое аттачмента.

        ``GET /api/testresult/attachment/{id}/content``

        Контент стримится чанками и обрезается на ``logs_max_attachment_bytes``,
        чтобы под не падал с OOM при больших логах.
        """
        logger.debug("Скачивание аттачмента %d", attachment_id)
        return await self._request_raw(
            "GET",
            f"{self.ATTACHMENT_ENDPOINT}/{attachment_id}/content",
            max_bytes=self._max_attachment_bytes,
        )

    # --- Внутренний HTTP ---

    async def _send_with_auth_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Выполнить запрос с auth-header и одноразовым повтором при 401.

        Транслирует ``httpx.RequestError`` в ``AllureApiError(0, ...)`` как на
        первой, так и на повторной попытке. Возвращает ``httpx.Response`` —
        вызывающий код сам решает, как интерпретировать тело и какие
        статусы считать ошибками.
        """
        url = f"{self._endpoint}{path}"
        auth_header = await self._auth.get_auth_header()

        try:
            resp = await self._http.request(
                method, url, params=params, json=json, headers=auth_header,
            )
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc

        if resp.status_code == 401:
            logger.debug("Получен 401, выполняем повторную аутентификацию и повтор запроса")
            failed_token = auth_header.get("Authorization", "").removeprefix("Bearer ")
            self._auth.invalidate(failed_token=failed_token)
            auth_header = await self._auth.get_auth_header()
            try:
                resp = await self._http.request(
                    method, url, params=params, json=json, headers=auth_header,
                )
            except httpx.RequestError as exc:
                raise AllureApiError(0, str(exc), path) from exc

        return resp

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """Выполнить аутентифицированный HTTP-запрос, возвращая сырые байты.

        Используется для скачивания аттачментов (бинарные файлы, текстовые логи).
        Ответ читается чанками; если задан ``max_bytes`` и контент превышает
        лимит — лишнее отбрасывается, в лог пишется warning. Это защищает
        память пода от очень больших логов.
        """
        logger.debug(
            "HTTP raw запрос (stream): %s %s%s max_bytes=%s",
            method, self._endpoint, path, max_bytes,
        )
        url = f"{self._endpoint}{path}"
        auth_header = await self._auth.get_auth_header()

        chunks, total, capped, status, body_preview = await self._stream_attempt(
            method, url, params, auth_header, max_bytes, path,
        )

        if status == 401:
            logger.debug("Получен 401 (stream), reauth и повтор")
            failed_token = auth_header.get("Authorization", "").removeprefix("Bearer ")
            self._auth.invalidate(failed_token=failed_token)
            auth_header = await self._auth.get_auth_header()
            chunks, total, capped, status, body_preview = await self._stream_attempt(
                method, url, params, auth_header, max_bytes, path,
            )

        if status == 404:
            raise AllureApiError(
                404,
                f"Аттачмент не найден. Проверьте Swagger UI: "
                f"{self._endpoint}/swagger-ui.html",
                path,
            )

        if status >= 400:
            raise AllureApiError(status, body_preview, path)

        if capped:
            logger.warning(
                "Аттачмент %s обрезан: прочитано %d байт, превышен лимит %d",
                path, total, max_bytes,
            )

        return b"".join(chunks)

    async def _stream_attempt(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str],
        max_bytes: int | None,
        path: str,
    ) -> tuple[list[bytes], int, bool, int, str]:
        """Одна попытка streaming-запроса. Возвращает кортеж
        ``(chunks, total_bytes, capped, status_code, body_preview)``.

        ``httpx.RequestError`` транслируется в ``AllureApiError(0, ...)``.
        Для статусов 4xx/5xx тело читается целиком в ``body_preview`` (≤500 байт)
        для понятного сообщения об ошибке.
        """
        # Маленький cap на тело ошибки. Прокси/балансировщики или сам TestOps
        # могут отдать огромную HTML-страницу/JSON на 5xx; нельзя позволить
        # error-пути обойти memory cap, который мы только что ввели для успешного.
        # 1 KiB более чем достаточно для понятного сообщения об ошибке
        # (decode → срез ≤500 символов далее).
        ERROR_BODY_PREVIEW_BYTES = 1024

        try:
            async with self._http.stream(
                method, url, params=params, headers=headers,
            ) as resp:
                status = resp.status_code
                if status >= 400:
                    preview_buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        remaining = ERROR_BODY_PREVIEW_BYTES - len(preview_buf)
                        if remaining <= 0:
                            break
                        preview_buf.extend(chunk[:remaining])
                        if len(preview_buf) >= ERROR_BODY_PREVIEW_BYTES:
                            break
                    preview = bytes(preview_buf).decode("utf-8", errors="replace")[:500]
                    return [], 0, False, status, preview

                chunks: list[bytes] = []
                total = 0
                capped = False
                async for chunk in resp.aiter_bytes():
                    if max_bytes is not None and total + len(chunk) > max_bytes:
                        remaining = max_bytes - total
                        if remaining > 0:
                            chunks.append(chunk[:remaining])
                            total += remaining
                        capped = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return chunks, total, capped, status, ""
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc

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
        """
        logger.debug(
            "HTTP-запрос: %s %s%s параметры=%s json=%s",
            method, self._endpoint, path, params, json,
        )
        resp = await self._send_with_auth_retry(method, path, params=params, json=json)

        logger.debug(
            "HTTP-ответ: %s %s status=%d content_length=%d",
            method, path, resp.status_code, len(resp.content),
        )

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
            return resp.json()
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

    async def __aenter__(self) -> "AllureTestOpsClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
