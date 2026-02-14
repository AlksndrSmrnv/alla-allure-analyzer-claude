"""Тесты JWT-аутентификации AllureAuthManager: обмен токенов, кэширование, ошибки."""

from __future__ import annotations

import time

import httpx
import pytest

from alla.clients.auth import AllureAuthManager, _TokenInfo
from alla.exceptions import AuthenticationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockResponse:
    """Заглушка для httpx.Response."""

    def __init__(
        self,
        json_data: dict | None = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json_data = json_data
        self.status_code = status_code
        self.text = text
        # Для raise_for_status:
        self.request = httpx.Request("POST", "https://test/api/uaa/oauth/token")

    def json(self):
        if self._json_data is None:
            raise ValueError("Invalid JSON")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = httpx.Response(
                status_code=self.status_code,
                request=self.request,
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=resp,
            )


def _make_auth(mock_http) -> AllureAuthManager:
    """Создать AllureAuthManager с подменённым HTTP-клиентом."""
    auth = AllureAuthManager(
        endpoint="https://allure.test",
        api_token="test-api-token",
    )
    auth._http = mock_http
    return auth


# ---------------------------------------------------------------------------
# _TokenInfo
# ---------------------------------------------------------------------------


def test_token_info_not_expired_when_fresh() -> None:
    """Свежий токен (expires_in=3600) → is_expired=False."""
    token = _TokenInfo(access_token="jwt", expires_in=3600)
    assert token.is_expired is False


def test_token_info_expired_within_buffer(monkeypatch) -> None:
    """Токен истекает через 4 мин (< 5 мин buffer) → is_expired=True."""
    token = _TokenInfo(access_token="jwt", expires_in=3600)
    # Симулируем: прошло 3400 секунд, осталось 200 < 300 (buffer)
    monkeypatch.setattr(time, "time", lambda: token.obtained_at + 3400)
    assert token.is_expired is True


def test_token_info_not_expired_outside_buffer(monkeypatch) -> None:
    """Токен истекает через 6 мин (> 5 мин buffer) → is_expired=False."""
    token = _TokenInfo(access_token="jwt", expires_in=3600)
    monkeypatch.setattr(time, "time", lambda: token.obtained_at + 3000)
    assert token.is_expired is False


# ---------------------------------------------------------------------------
# _exchange_token — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_token_success() -> None:
    """Успешный обмен: парсинг access_token и expires_in."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse({"access_token": "jwt-123", "expires_in": 7200})

    auth = _make_auth(_Http())
    token_info = await auth._exchange_token()

    assert token_info.access_token == "jwt-123"
    assert token_info.expires_in == 7200


@pytest.mark.asyncio
async def test_exchange_token_defaults_expires_in() -> None:
    """Ответ без expires_in → дефолт 3600 секунд."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse({"access_token": "jwt-456"})

    auth = _make_auth(_Http())
    token_info = await auth._exchange_token()

    assert token_info.expires_in == 3600


# ---------------------------------------------------------------------------
# _exchange_token — ошибки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_token_http_error_raises_auth_error() -> None:
    """HTTPStatusError (401) → AuthenticationError."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse(status_code=401, text="Unauthorized")

    auth = _make_auth(_Http())

    with pytest.raises(AuthenticationError, match="HTTP 401"):
        await auth._exchange_token()


@pytest.mark.asyncio
async def test_exchange_token_network_error_raises_auth_error() -> None:
    """RequestError (сеть) → AuthenticationError."""

    class _Http:
        async def post(self, url, data, headers):
            raise httpx.ConnectError("Connection refused")

    auth = _make_auth(_Http())

    with pytest.raises(AuthenticationError, match="Ошибка запроса"):
        await auth._exchange_token()


@pytest.mark.asyncio
async def test_exchange_token_invalid_json_raises_auth_error() -> None:
    """Ответ не JSON → AuthenticationError."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse(json_data=None, text="<html>not json</html>")

    auth = _make_auth(_Http())

    with pytest.raises(AuthenticationError, match="не является валидным JSON"):
        await auth._exchange_token()


@pytest.mark.asyncio
async def test_exchange_token_missing_access_token_raises_auth_error() -> None:
    """JSON без access_token → AuthenticationError."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse({"token_type": "bearer"})

    auth = _make_auth(_Http())

    with pytest.raises(AuthenticationError, match="access_token"):
        await auth._exchange_token()


# ---------------------------------------------------------------------------
# get_auth_header — кэширование
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_auth_header_caches_token() -> None:
    """Второй вызов использует кэш — HTTP POST вызывается только один раз."""
    call_count = 0

    class _Http:
        async def post(self, url, data, headers):
            nonlocal call_count
            call_count += 1
            return _MockResponse({"access_token": "jwt", "expires_in": 3600})

    auth = _make_auth(_Http())

    header1 = await auth.get_auth_header()
    header2 = await auth.get_auth_header()

    assert call_count == 1
    assert header1 == header2 == {"Authorization": "Bearer jwt"}


@pytest.mark.asyncio
async def test_get_auth_header_refreshes_expired_token(monkeypatch) -> None:
    """Истекший токен → повторный обмен."""
    call_count = 0

    class _Http:
        async def post(self, url, data, headers):
            nonlocal call_count
            call_count += 1
            return _MockResponse({"access_token": f"jwt-{call_count}", "expires_in": 3600})

    auth = _make_auth(_Http())

    await auth.get_auth_header()
    assert call_count == 1

    # Симулируем истечение токена
    monkeypatch.setattr(time, "time", lambda: auth._token_info.obtained_at + 3400)
    header = await auth.get_auth_header()

    assert call_count == 2
    assert header == {"Authorization": "Bearer jwt-2"}


@pytest.mark.asyncio
async def test_get_auth_header_returns_bearer_format() -> None:
    """Заголовок имеет формат Authorization: Bearer <token>."""

    class _Http:
        async def post(self, url, data, headers):
            return _MockResponse({"access_token": "my-jwt-token"})

    auth = _make_auth(_Http())
    header = await auth.get_auth_header()

    assert header == {"Authorization": "Bearer my-jwt-token"}


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_clears_cache() -> None:
    """invalidate() → следующий вызов делает новый обмен."""
    call_count = 0

    class _Http:
        async def post(self, url, data, headers):
            nonlocal call_count
            call_count += 1
            return _MockResponse({"access_token": f"jwt-{call_count}"})

    auth = _make_auth(_Http())

    await auth.get_auth_header()
    assert call_count == 1

    auth.invalidate()
    header = await auth.get_auth_header()

    assert call_count == 2
    assert header == {"Authorization": "Bearer jwt-2"}


# ---------------------------------------------------------------------------
# Конструктор
# ---------------------------------------------------------------------------


def test_constructor_strips_trailing_slash() -> None:
    """endpoint с trailing slash → внутри хранится без slash."""
    auth = AllureAuthManager(
        endpoint="https://allure.test/",
        api_token="token",
    )
    assert auth._endpoint == "https://allure.test"
