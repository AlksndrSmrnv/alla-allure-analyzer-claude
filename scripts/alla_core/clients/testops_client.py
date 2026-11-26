"""REST-клиент Allure TestOps — только запись статуса результата (resolve).

Читает скилл через MCP (``clients/mcp_client.py``): REST API у пользователей закрыт. Здесь
осталась единственная запись, которой в MCP нет, — статус passed после перезапуска
(``ALLURE_RERUN_RESOLVE``, по умолчанию выключено): на случай, когда REST открыт.
"""

import logging
from typing import Any

import httpx

from alla_core.clients.auth import AllureAuthManager
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError
from alla_core.origins import require_allowed_origin

logger = logging.getLogger(__name__)


class AllureTestOpsClient:
    """REST-клиент TestOps для ``POST /api/testresult/{id}/resolve``."""

    TESTRESULT_ENDPOINT = "/api/testresult"

    def __init__(self, settings: Settings, auth_manager: AllureAuthManager) -> None:
        settings.validate_testops_origins()
        self._allowed_origins = settings.allowed_origins
        self._endpoint = str(settings.endpoint).strip().rstrip("/")
        self._auth = auth_manager
        self._http = httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False)

    async def resolve_test_result(
        self, test_result_id: int, status: str, message: str,
    ) -> dict[str, Any]:
        """Сменить статус одного результата теста (ручное «Resolve» TestOps).

        ``POST /api/testresult/{id}/resolve`` с ``ResolveRequestV2Dto`` (OpenAPI 26.1.1),
        ответ ``202`` — строка результата (``TestResultRowDto``: ``id``, ``status``…). Запрос
        на один id, а не ``bulk/resolve``: выборка дерева при ошибке задела бы лишние
        результаты. Не повторяется: запись. Успех — только 2xx: перенаправление (прокси,
        страница входа) статус не меняло, хотя и не ошибка HTTP. Тело пустое или не JSON — ``{}``.
        """
        path = f"{self.TESTRESULT_ENDPOINT}/{test_result_id}/resolve"
        resp = await self._send("POST", path, json={"status": status, "message": message})
        if 300 <= resp.status_code < 400:
            raise AllureApiError(resp.status_code, "перенаправление: запрос не дошёл до TestOps", path)
        if not 200 <= resp.status_code < 300:
            raise AllureApiError(resp.status_code, resp.text[:500], path)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _send(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Запрос с заголовком авторизации и одноразовым повтором при 401 (JWT истёк).

        ``httpx.RequestError`` — ``AllureApiError(0, ...)``.
        """
        url = f"{self._endpoint}{path}"
        require_allowed_origin(url, self._allowed_origins)
        auth_header = await self._auth.get_auth_header()
        try:
            resp = await self._http.request(method, url, json=json, headers=auth_header)
        except httpx.RequestError as exc:
            raise AllureApiError(0, str(exc), path) from exc
        if resp.status_code == 401:
            logger.debug("Получен 401, выполняем повторную аутентификацию и повтор запроса")
            failed_token = auth_header.get("Authorization", "").removeprefix("Bearer ")
            self._auth.invalidate(failed_token=failed_token)
            auth_header = await self._auth.get_auth_header()
            try:
                resp = await self._http.request(method, url, json=json, headers=auth_header)
            except httpx.RequestError as exc:
                raise AllureApiError(0, str(exc), path) from exc
        return resp

    async def close(self) -> None:
        """Освободить ресурсы HTTP-клиента и менеджера аутентификации."""
        await self._http.aclose()
        await self._auth.close()

    async def __aenter__(self) -> "AllureTestOpsClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
