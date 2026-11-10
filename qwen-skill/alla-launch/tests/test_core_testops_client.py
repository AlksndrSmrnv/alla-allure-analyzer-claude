"""Поведенческие тесты ``AllureTestOpsClient`` — REST-запись статуса (resolve): auth, 401, ответы."""

from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path
from alla_core.clients.auth import AllureAuthManager
from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError


async def _make_client(*, fresh_only: bool = False) -> tuple[AllureTestOpsClient, MagicMock]:
    settings = Settings(endpoint="https://allure.test", token="test-token",
                        allowed_origins=("https://allure.test",))
    auth = MagicMock(spec=AllureAuthManager)
    if fresh_only:
        auth.get_auth_header = AsyncMock(return_value={"Authorization": "Bearer fresh"})
    else:
        # Очерёдность токенов: первый протух, второй валиден.
        auth.get_auth_header = AsyncMock(side_effect=[
            {"Authorization": "Bearer stale"},
            {"Authorization": "Bearer fresh"},
            {"Authorization": "Bearer fresh"},
        ])
    client = AllureTestOpsClient(settings, auth)
    # Конструктор поднимает реальный httpx.AsyncClient — закрываем его перед
    # подменой на _ScriptedHttp, иначе остаются ResourceWarning'и.
    await client._http.aclose()
    return client, auth


class _ScriptedHttp:
    """httpx-замена, отдающая по очереди заранее прописанные реакции (Exception — поднимается)."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, *, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("Неожиданный запрос — сценарий исчерпан")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        pass


def _make_response(status_code: int, *, body: bytes = b"", text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = body
    resp.text = text or body.decode("utf-8", errors="replace")
    if body:
        try:
            resp.json.return_value = _json.loads(body)
        except Exception:
            resp.json.side_effect = ValueError("not json")
    else:
        resp.json.side_effect = ValueError("empty body")
    return resp


@pytest.mark.asyncio
async def test_resolve_retries_once_on_401_with_a_fresh_token() -> None:
    """401 → invalidate токена → повтор с новым JWT."""
    client, auth = await _make_client()
    client._http = _ScriptedHttp([
        _make_response(401, text="expired"),
        _make_response(202, body=b'{"id": 7, "status": "passed"}'),
    ])

    assert await client.resolve_test_result(7, "passed", "alla") == {"id": 7, "status": "passed"}
    auth.invalidate.assert_called_once_with(failed_token="stale")
    headers_used = [call["headers"]["Authorization"] for call in client._http.calls]
    assert headers_used == ["Bearer stale", "Bearer fresh"]


@pytest.mark.asyncio
async def test_resolve_raises_when_retry_also_returns_401() -> None:
    client, _ = await _make_client()
    client._http = _ScriptedHttp([_make_response(401, text="first"),
                                  _make_response(401, text="still bad")])

    with pytest.raises(AllureApiError) as exc_info:
        await client.resolve_test_result(7, "passed", "alla")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [
    [httpx.ConnectError("connection refused")],
    [_make_response(401, text="expired"), httpx.ConnectError("connection refused")],
])
async def test_resolve_translates_network_errors(script: list[object]) -> None:
    """httpx.RequestError на первой попытке и на повторе после 401 → AllureApiError(0, ...)."""
    client, _ = await _make_client()
    client._http = _ScriptedHttp(script)

    with pytest.raises(AllureApiError) as exc_info:
        await client.resolve_test_result(7, "passed", "alla")

    assert exc_info.value.status_code == 0
    assert "connection refused" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolve_posts_one_result_and_is_not_retried() -> None:
    """Статус меняется запросом на один результат; 202 без тела — не ошибка, 502 — без повтора."""
    client, _ = await _make_client(fresh_only=True)
    client._http = _ScriptedHttp([
        _make_response(202, body=b'{"id": 9, "status": "passed"}'),
        _make_response(202),
        _make_response(502, text="bad gateway"),
    ])

    assert await client.resolve_test_result(9, "passed", "alla: прошёл") == {"id": 9, "status": "passed"}
    assert await client.resolve_test_result(9, "passed", "alla: прошёл") == {}
    with pytest.raises(AllureApiError) as exc_info:
        await client.resolve_test_result(9, "passed", "alla: прошёл")

    assert exc_info.value.status_code == 502
    first = client._http.calls[0]
    assert (first["method"], first["url"], first["json"]) == (
        "POST", "https://allure.test/api/testresult/9/resolve",
        {"status": "passed", "message": "alla: прошёл"})
    assert len(client._http.calls) == 3


@pytest.mark.asyncio
async def test_resolve_counts_only_2xx_as_done() -> None:
    """Перенаправление прокси или входа — не успех, хотя и не ошибка HTTP; 2xx не JSON — {}."""
    client, _ = await _make_client(fresh_only=True)
    client._http = _ScriptedHttp([_make_response(302), _make_response(202, body=b"<html>ok</html>")])

    with pytest.raises(AllureApiError) as exc_info:
        await client.resolve_test_result(9, "passed", "alla")
    assert exc_info.value.status_code == 302
    assert await client.resolve_test_result(9, "passed", "alla") == {}


def test_http_retries_setting_bounds() -> None:
    from alla_core.exceptions import ConfigurationError

    base = {"ALLURE_ENDPOINT": "https://allure.test", "ALLURE_TOKEN": "t"}
    assert Settings.load(require_testops=False, environ=base).http_retries == 3
    assert Settings.load(require_testops=False, environ={**base, "ALLURE_HTTP_RETRIES": "0"}).http_retries == 0
    with pytest.raises(ConfigurationError, match="ALLURE_HTTP_RETRIES"):
        Settings.load(require_testops=False, environ={**base, "ALLURE_HTTP_RETRIES": "11"})
