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


async def _make_client(monkeypatch, tmp_path) -> tuple[AllureTestOpsClient, MagicMock]:
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
    # Конструктор поднимает реальный httpx.AsyncClient — закрываем его перед
    # подменой на _ScriptedHttp, иначе остаются ResourceWarning'и.
    await client._http.aclose()
    return client, auth


class _ScriptedHttp:
    """httpx-замена, отдающая по очереди заранее прописанные реакции.

    Поддерживает два режима:

    - ``request(...)`` — для JSON-запросов; возвращает следующий элемент из
      очереди (либо raises, если элемент — Exception).
    - ``stream(...)`` — для бинарных/raw-запросов; возвращает async-context
      manager поверх следующего элемента очереди. Если элемент — Exception,
      исключение поднимается при входе в ``async with`` (это эквивалент
      ``httpx.RequestError`` из реального клиента).
    """

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

    def stream(self, method, url, *, params=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers, "stream": True},
        )
        if not self._responses:
            raise AssertionError("Неожиданный stream-запрос — сценарий исчерпан")
        item = self._responses.pop(0)
        return _StreamCtx(item)

    async def aclose(self) -> None:
        pass


class _StreamCtx:
    """Async context manager поверх scripted-элемента для ``_ScriptedHttp.stream``."""

    def __init__(self, item: object) -> None:
        self._item = item

    async def __aenter__(self):
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _StreamResponse:
    """Лёгкий аналог ``httpx.Response`` для streaming-тестов.

    ``aiter_bytes()`` отдаёт чанки. Для error-кейсов (>=400) передавайте
    ``error_body`` — он будет отдан как один чанк через ``aiter_bytes``
    (production-код читает preview оттуда же, со своим bytes-лимитом).
    """

    def __init__(
        self,
        status_code: int,
        *,
        chunks: list[bytes] | None = None,
        error_body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        if chunks is not None:
            self._chunks: list[bytes] = list(chunks)
        elif error_body:
            self._chunks = [error_body]
        else:
            self._chunks = []
        self._error_body = error_body

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aread(self) -> bytes:
        return self._error_body


def _make_stream_response(
    status_code: int,
    *,
    chunks: list[bytes] | None = None,
    error_body: bytes = b"",
) -> _StreamResponse:
    return _StreamResponse(status_code, chunks=chunks, error_body=error_body)


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
    client, auth = await _make_client(monkeypatch, tmp_path)
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
    client, _ = await _make_client(monkeypatch, tmp_path)
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
    client, _ = await _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([httpx.ConnectError("connection refused")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 0
    assert "connection refused" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_translates_request_error_on_retry(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на повторной попытке после 401 → AllureApiError(0, ...)."""
    client, _ = await _make_client(monkeypatch, tmp_path)
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
    client, _ = await _make_client(monkeypatch, tmp_path)
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
    client, auth = await _make_client(monkeypatch, tmp_path)
    payload = b"\x89PNG\r\n\x1a\nbinary"
    client._http = _ScriptedHttp(
        [
            _make_stream_response(401, error_body=b"expired"),
            _make_stream_response(200, chunks=[payload]),
        ]
    )

    result = await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert result == payload
    auth.invalidate.assert_called_once_with(failed_token="stale")


@pytest.mark.asyncio
async def test_request_raw_translates_request_error_on_first_attempt(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на первой попытке raw-запроса → AllureApiError(0, ...)."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([httpx.ReadTimeout("timeout")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 0


@pytest.mark.asyncio
async def test_request_raw_translates_request_error_on_retry(monkeypatch, tmp_path) -> None:
    """httpx.RequestError на повторной попытке raw-запроса → AllureApiError(0, ...)."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp(
        [
            _make_stream_response(401, error_body=b"expired"),
            httpx.ReadTimeout("retry timeout"),
        ]
    )

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 0


@pytest.mark.asyncio
async def test_request_raw_streams_multiple_chunks(monkeypatch, tmp_path) -> None:
    """Без max_bytes: все чанки склеиваются в полный bytes-результат."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    chunks = [b"AAAA", b"BBBB", b"CCCC"]
    client._http = _ScriptedHttp([_make_stream_response(200, chunks=chunks)])

    result = await client._request_raw(
        "GET", "/api/testresult/attachment/1/content",
    )

    assert result == b"AAAABBBBCCCC"


@pytest.mark.asyncio
async def test_request_raw_caps_at_max_bytes(monkeypatch, tmp_path) -> None:
    """С max_bytes: чтение прерывается, лишние чанки отбрасываются."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    # 3 чанка по 4 байта = 12 байт; max_bytes=6 — должен забрать ровно 6.
    chunks = [b"AAAA", b"BBBB", b"CCCC"]
    client._http = _ScriptedHttp([_make_stream_response(200, chunks=chunks)])

    result = await client._request_raw(
        "GET",
        "/api/testresult/attachment/1/content",
        max_bytes=6,
    )

    assert result == b"AAAABB"
    assert len(result) == 6


@pytest.mark.asyncio
async def test_request_raw_404_carries_swagger_hint(monkeypatch, tmp_path) -> None:
    """404 на streaming-запросе даёт подсказку про Swagger."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([_make_stream_response(404, error_body=b"missing")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 404
    assert "swagger-ui" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_request_raw_caps_error_body_preview(monkeypatch, tmp_path) -> None:
    """Большое тело 5xx-ответа НЕ читается целиком — preview ≤500 символов."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    # 100 чанков по 4 KiB = 400 KiB предполагаемого тела ошибки.
    # Production-код должен остановиться через ≤2 чанка.
    huge_chunks = [b"E" * 4096 for _ in range(100)]
    client._http = _ScriptedHttp(
        [_StreamResponse(500, chunks=huge_chunks)]
    )

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 500
    # Сообщение об ошибке несёт ≤500 символов превью.
    message = str(exc_info.value)
    # Префикс "HTTP 500 от ..." + сам preview. Хотим, чтобы общий объём был
    # разумным; основной критерий — что не висит 400 KiB в строке.
    assert len(message) < 2000


@pytest.mark.asyncio
async def test_get_attachment_content_applies_settings_cap(monkeypatch, tmp_path) -> None:
    """``get_attachment_content`` передаёт ``logs_max_attachment_bytes`` в raw."""
    client, _ = await _make_client(monkeypatch, tmp_path)
    client._max_attachment_bytes = 5
    chunks = [b"hello", b"world!!!!"]
    client._http = _ScriptedHttp([_make_stream_response(200, chunks=chunks)])

    result = await client.get_attachment_content(42)

    assert result == b"hello"
