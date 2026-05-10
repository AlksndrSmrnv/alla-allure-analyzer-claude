"""Поведенческие тесты ``AllureTestOpsClient`` — auth + 401 retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from alla.clients.auth import AllureAuthManager
from alla.clients.testops_client import AllureTestOpsClient
from alla.config import Settings
from alla.exceptions import AllureApiError


def _make_settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.test")
    monkeypatch.setenv("ALLURE_TOKEN", "test-token")
    return Settings()


def _make_client(monkeypatch, tmp_path) -> tuple[AllureTestOpsClient, MagicMock]:
    settings = _make_settings(monkeypatch, tmp_path)
    auth = MagicMock(spec=AllureAuthManager)
    # Очерёдность токенов: первый протух, второй валиден.
    auth.get_auth_header = AsyncMock(
        side_effect=[
            {"Authorization": "Bearer stale"},
            {"Authorization": "Bearer fresh"},
            {"Authorization": "Bearer fresh"},
        ]
    )
    client = AllureTestOpsClient(settings, auth)
    return client, auth


class _ScriptedHttp:
    """httpx-замена, отдающая по очереди заранее прописанные реакции."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, *, params=None, json=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers},
        )
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
            import json as _json
            resp.json.return_value = _json.loads(body)
        except Exception:
            resp.json.side_effect = ValueError("not json")
    else:
        resp.json.side_effect = ValueError("empty body")
    return resp


# ---------------------------------------------------------------------------
# _request: 401 retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_retries_on_401_and_returns_json(monkeypatch, tmp_path) -> None:
    """401 → invalidate токена → повтор → возвращён JSON ответа."""
    client, auth = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp(
        [
            _make_response(401, text="expired"),
            _make_response(200, body=b'{"id": 7}'),
        ]
    )

    result = await client._request("GET", "/api/launch/7")

    assert result == {"id": 7}
    auth.invalidate.assert_called_once_with(failed_token="stale")
    assert auth.get_auth_header.await_count == 2
    # Первая попытка — со старым токеном, вторая — с новым.
    headers_used = [call["headers"]["Authorization"] for call in client._http.calls]
    assert headers_used == ["Bearer stale", "Bearer fresh"]


@pytest.mark.asyncio
async def test_request_raises_when_retry_also_returns_401(monkeypatch, tmp_path) -> None:
    """Если повтор тоже вернул 401, поднимаем AllureApiError(401, ...)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp(
        [
            _make_response(401, text="first"),
            _make_response(401, text="still bad"),
        ]
    )

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_request_translates_request_error_on_first_attempt(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на первой попытке → AllureApiError(0, ...)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([httpx.ConnectError("connection refused")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 0
    assert "connection refused" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_translates_request_error_on_retry(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на повторной попытке после 401 → AllureApiError(0, ...)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp(
        [
            _make_response(401, text="expired"),
            httpx.ConnectError("retry failed"),
        ]
    )

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 0
    assert "retry failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_404_carries_swagger_hint(monkeypatch, tmp_path) -> None:
    """404 даёт человеко-читаемое сообщение с ссылкой на Swagger."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([_make_response(404, text="missing")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/999")

    assert exc_info.value.status_code == 404
    assert "swagger-ui" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# _request_raw: 401 retry для бинарных ответов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_raw_retries_on_401_and_returns_bytes(monkeypatch, tmp_path) -> None:
    """401 → invalidate → повтор → возвращены бинарные байты."""
    client, auth = _make_client(monkeypatch, tmp_path)
    payload = b"\x89PNG\r\n\x1a\nbinary"
    client._http = _ScriptedHttp(
        [
            _make_response(401, text="expired"),
            _make_response(200, body=payload),
        ]
    )

    result = await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert result == payload
    auth.invalidate.assert_called_once_with(failed_token="stale")


@pytest.mark.asyncio
async def test_request_raw_translates_request_error_on_first_attempt(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на первой попытке raw-запроса → AllureApiError(0, ...)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([httpx.ReadTimeout("timeout")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 0


@pytest.mark.asyncio
async def test_request_raw_translates_request_error_on_retry(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на повторной попытке raw-запроса → AllureApiError(0, ...)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp(
        [
            _make_response(401, text="expired"),
            httpx.ReadTimeout("retry timeout"),
        ]
    )

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 0
