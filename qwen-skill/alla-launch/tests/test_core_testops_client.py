"""Поведенческие тесты ``AllureTestOpsClient`` — auth + 401 retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path
from alla_core.clients.auth import AllureAuthManager
from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError


def _make_settings(monkeypatch, tmp_path, *, retries: int = 0) -> Settings:
    # Settings скилла — dataclass: окружение читает только Settings.load,
    # а тесту нужны фиксированные значения. Повторы при 5xx и сбоях сети по умолчанию
    # выключены: эти тесты проверяют одну попытку; повторы — в разделе ниже.
    return Settings(endpoint="https://allure.test", token="test-token", http_retries=retries)


async def _make_client(
    monkeypatch, tmp_path, *, retries: int = 0,
) -> tuple[AllureTestOpsClient, MagicMock]:
    settings = _make_settings(monkeypatch, tmp_path, retries=retries)
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
    client._sleep = _RecordedSleep()  # type: ignore[assignment]
    return client, auth


class _RecordedSleep:
    """Замена ``asyncio.sleep`` клиента: паузы перед повторами без ожидания."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


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


# ---------------------------------------------------------------------------
# Повторы при временных сбоях (429, 502–504, сеть)
# ---------------------------------------------------------------------------


async def _retrying_client(monkeypatch, tmp_path, retries: int = 3) -> AllureTestOpsClient:
    client, auth = await _make_client(monkeypatch, tmp_path, retries=retries)
    auth.get_auth_header = AsyncMock(return_value={"Authorization": "Bearer fresh"})
    return client


def _with_header(resp: MagicMock, name: str, value: str) -> MagicMock:
    resp.headers = httpx.Headers({name: value})
    return resp


@pytest.mark.asyncio
async def test_request_retries_bad_gateway_and_returns_json(monkeypatch, tmp_path) -> None:
    """Один 502 балансировщика не обрывает выгрузку: пауза 1 с и повтор."""
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([
        _make_response(502, text="bad gateway"),
        _make_response(200, body=b'{"id": 7}'),
    ])

    assert await client._request("GET", "/api/launch/7") == {"id": 7}
    assert client._sleep.delays == [1.0]


@pytest.mark.asyncio
async def test_request_retry_after_sets_the_pause(monkeypatch, tmp_path) -> None:
    """429 с Retry-After — пауза по заголовку; больше 30 с не ждём."""
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([
        _with_header(_make_response(429, text="slow down"), "Retry-After", "5"),
        _with_header(_make_response(429, text="slow down"), "Retry-After", "3600"),
        _with_header(
            _make_response(503, text="busy"), "Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT",
        ),
        _make_response(200, body=b'{"id": 7}'),
    ])

    assert await client._request("GET", "/api/launch/7") == {"id": 7}
    # Дату HTTP не разбираем — обычная пауза третьей попытки.
    assert client._sleep.delays == [5.0, 30.0, 4.0]


@pytest.mark.asyncio
async def test_request_retries_network_errors_with_growing_pause(monkeypatch, tmp_path) -> None:
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout(""),
        _make_response(200, body=b'{"id": 7}'),
    ])

    assert await client._request("GET", "/api/launch/7") == {"id": 7}
    assert client._sleep.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_request_gives_up_after_configured_retries(monkeypatch, tmp_path) -> None:
    """После ``http_retries`` повторов — ошибка последнего ответа."""
    client = await _retrying_client(monkeypatch, tmp_path, retries=2)
    client._http = _ScriptedHttp([_make_response(503, text="busy")] * 3)

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 503
    assert len(client._http.calls) == 3
    assert client._sleep.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_request_gives_up_on_persistent_network_error(monkeypatch, tmp_path) -> None:
    client = await _retrying_client(monkeypatch, tmp_path, retries=1)
    client._http = _ScriptedHttp([httpx.ConnectError("down"), httpx.ConnectError("still down")])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")

    assert exc_info.value.status_code == 0
    assert "still down" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_does_not_retry_internal_error_or_post(monkeypatch, tmp_path) -> None:
    """500 TestOps обычно воспроизводится, а не-GET не идемпотентен — без повторов."""
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([_make_response(500, text="boom")])
    with pytest.raises(AllureApiError) as exc_info:
        await client._request("GET", "/api/launch/7")
    assert exc_info.value.status_code == 500

    client._http = _ScriptedHttp([_make_response(502, text="bad gateway")])
    resp = await client._send_with_auth_retry("POST", "/api/whatever")
    assert resp.status_code == 502
    assert client._sleep.delays == []


@pytest.mark.asyncio
async def test_resolve_posts_one_result_and_is_not_retried(monkeypatch, tmp_path) -> None:
    """Статус меняется запросом на один результат; 202 без тела — не ошибка, 502 — без повтора."""
    client = await _retrying_client(monkeypatch, tmp_path)
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
    assert len(client._http.calls) == 3 and client._sleep.delays == []


@pytest.mark.asyncio
async def test_resolve_counts_only_2xx_as_done(monkeypatch, tmp_path) -> None:
    """Перенаправление прокси или входа — не успех, хотя и не ошибка HTTP; 2xx не JSON — {}."""
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([_make_response(302), _make_response(202, body=b"<html>ok</html>")])

    with pytest.raises(AllureApiError) as exc_info:
        await client.resolve_test_result(9, "passed", "alla")
    assert exc_info.value.status_code == 302
    assert await client.resolve_test_result(9, "passed", "alla") == {}


@pytest.mark.asyncio
async def test_request_raw_retries_gateway_and_network_errors(monkeypatch, tmp_path) -> None:
    client = await _retrying_client(monkeypatch, tmp_path)
    client._http = _ScriptedHttp([
        _make_stream_response(504, error_body=b"gateway timeout"),
        httpx.ReadTimeout("timeout"),
        _make_stream_response(200, chunks=[b"log"]),
    ])

    assert await client._request_raw("GET", "/api/testresult/attachment/1/content") == b"log"
    assert client._sleep.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_request_raw_gives_up_after_retries(monkeypatch, tmp_path) -> None:
    client = await _retrying_client(monkeypatch, tmp_path, retries=1)
    client._http = _ScriptedHttp([
        _make_stream_response(502, error_body=b"bad gateway"),
        _make_stream_response(502, error_body=b"bad gateway"),
    ])

    with pytest.raises(AllureApiError) as exc_info:
        await client._request_raw("GET", "/api/testresult/attachment/1/content")

    assert exc_info.value.status_code == 502
    assert len(client._http.calls) == 2


def test_http_retries_setting_bounds() -> None:
    from alla_core.exceptions import ConfigurationError

    base = {"ALLURE_ENDPOINT": "https://allure.test", "ALLURE_TOKEN": "t"}
    assert Settings.load(environ=base).http_retries == 3
    assert Settings.load(environ={**base, "ALLURE_HTTP_RETRIES": "0"}).http_retries == 0
    with pytest.raises(ConfigurationError, match="ALLURE_HTTP_RETRIES"):
        Settings.load(environ={**base, "ALLURE_HTTP_RETRIES": "11"})
