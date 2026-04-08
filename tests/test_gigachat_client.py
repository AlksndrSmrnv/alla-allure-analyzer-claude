"""Тесты клиента GigaChatClient: запросы, ошибки, retry, парсинг ответов."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from alla.clients.gigachat_client import GigaChatClient, _extract_retry_after, _extract_status_code, _extract_token_usage
from alla.exceptions import LLMApiError
from alla.models.llm import TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _MockUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15


@dataclass
class _MockMessage:
    content: str = "LLM analysis result"


@dataclass
class _MockChoice:
    message: _MockMessage = field(default_factory=_MockMessage)


@dataclass
class _MockResponse:
    choices: list[_MockChoice] = field(default_factory=lambda: [_MockChoice()])
    usage: _MockUsage | None = field(default_factory=_MockUsage)


def _make_client(mock_giga: object) -> GigaChatClient:
    """Создать GigaChatClient с подменённым GigaChat SDK."""
    client = GigaChatClient.__new__(GigaChatClient)
    client._model = "test-model"
    client._base_url = "https://gigachat.test"
    client._max_retries = 3
    client._retry_base_delay = 0.01  # Быстрый retry для тестов
    client._giga = mock_giga
    return client


# ---------------------------------------------------------------------------
# chat — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_success() -> None:
    """Успешный запрос: текст извлечён из ответа GigaChat."""
    mock_giga = MagicMock()
    mock_giga.chat.return_value = _MockResponse(
        choices=[_MockChoice(_MockMessage("Hello LLM"))]
    )

    client = _make_client(mock_giga)
    result = await client.chat("system", "user input")

    assert result.text == "Hello LLM"
    mock_giga.chat.assert_called_once()


@pytest.mark.asyncio
async def test_chat_passes_system_and_user_messages() -> None:
    """Запрос содержит system и user сообщения."""
    captured_chat = None

    class _Giga:
        def chat(self, chat_request):
            nonlocal captured_chat
            captured_chat = chat_request
            return _MockResponse()

    client = _make_client(_Giga())
    await client.chat("system prompt", "user prompt")

    assert captured_chat is not None
    assert len(captured_chat.messages) == 2
    assert captured_chat.messages[0].content == "system prompt"
    assert captured_chat.messages[1].content == "user prompt"
    assert captured_chat.model == "test-model"
    assert captured_chat.stream is False


# ---------------------------------------------------------------------------
# chat — token usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_returns_token_usage() -> None:
    """Token usage извлекается из ответа GigaChat."""
    mock_giga = MagicMock()
    mock_giga.chat.return_value = _MockResponse(
        choices=[_MockChoice(_MockMessage("ok"))],
        usage=_MockUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )

    client = _make_client(mock_giga)
    result = await client.chat("sys", "user")

    assert result.token_usage.prompt_tokens == 100
    assert result.token_usage.completion_tokens == 50
    assert result.token_usage.total_tokens == 150


@pytest.mark.asyncio
async def test_chat_returns_zero_usage_when_missing() -> None:
    """Отсутствие usage → TokenUsage(0, 0, 0)."""
    mock_giga = MagicMock()
    mock_giga.chat.return_value = _MockResponse(
        choices=[_MockChoice(_MockMessage("ok"))],
        usage=None,
    )

    client = _make_client(mock_giga)
    result = await client.chat("sys", "user")

    assert result.token_usage == TokenUsage()


# ---------------------------------------------------------------------------
# _extract_token_usage
# ---------------------------------------------------------------------------


def test_extract_token_usage_with_valid_usage() -> None:
    """Извлечение usage из объекта с полями prompt/completion/total."""
    response = _MockResponse(usage=_MockUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30))
    usage = _extract_token_usage(response)
    assert usage == TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)


def test_extract_token_usage_none() -> None:
    """usage=None → TokenUsage(0, 0, 0)."""
    response = _MockResponse(usage=None)
    assert _extract_token_usage(response) == TokenUsage()


def test_extract_token_usage_no_attr() -> None:
    """Объект без атрибута usage → TokenUsage(0, 0, 0)."""
    assert _extract_token_usage(object()) == TokenUsage()


# ---------------------------------------------------------------------------
# chat — ошибки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_network_error_retries_and_raises() -> None:
    """Сетевая ошибка → retry → LLMApiError."""
    call_count = 0

    class _Giga:
        def chat(self, chat_request):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection refused")

    client = _make_client(_Giga())

    with pytest.raises(LLMApiError) as exc_info:
        await client.chat("sys", "user")

    assert exc_info.value.status_code == 0
    assert call_count == 4  # 1 + 3 retries


@pytest.mark.asyncio
async def test_chat_non_retryable_http_error_raises_immediately() -> None:
    """HTTP 400 → LLMApiError без retry."""
    call_count = 0

    class _HttpError(Exception):
        status_code = 400

    class _Giga:
        def chat(self, chat_request):
            nonlocal call_count
            call_count += 1
            raise _HttpError("Bad request")

    client = _make_client(_Giga())

    with pytest.raises(LLMApiError) as exc_info:
        await client.chat("sys", "user")

    assert exc_info.value.status_code == 400
    assert call_count == 1  # Без retry


@pytest.mark.asyncio
async def test_chat_retryable_status_code_retries() -> None:
    """HTTP 429 → retry."""
    call_count = 0

    class _HttpError(Exception):
        status_code = 429

    class _Giga:
        def chat(self, chat_request):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _HttpError("Too many requests")
            return _MockResponse(choices=[_MockChoice(_MockMessage("ok"))])

    client = _make_client(_Giga())
    result = await client.chat("sys", "user")

    assert result.text == "ok"
    assert call_count == 3


# ---------------------------------------------------------------------------
# chat — ошибки парсинга
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_empty_choices_raises() -> None:
    """Пустой choices → LLMApiError."""
    mock_giga = MagicMock()
    mock_giga.chat.return_value = _MockResponse(choices=[])

    client = _make_client(mock_giga)

    with pytest.raises(LLMApiError, match="Неожиданная структура"):
        await client.chat("sys", "user")


@pytest.mark.asyncio
async def test_chat_non_string_content_raises() -> None:
    """content=123 → LLMApiError."""
    mock_giga = MagicMock()
    mock_giga.chat.return_value = _MockResponse(
        choices=[_MockChoice(_MockMessage(content=123))]  # type: ignore[arg-type]
    )

    client = _make_client(mock_giga)

    with pytest.raises(LLMApiError, match="Ожидался str"):
        await client.chat("sys", "user")


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager() -> None:
    """async with GigaChatClient работает корректно."""
    client = _make_client(MagicMock())

    async with client as ctx:
        assert ctx is client


# ---------------------------------------------------------------------------
# _extract_status_code
# ---------------------------------------------------------------------------


def test_extract_status_code_from_attribute() -> None:
    """Извлечение status_code из атрибута исключения."""

    class _Exc(Exception):
        status_code = 503

    assert _extract_status_code(_Exc()) == 503


def test_extract_status_code_from_response() -> None:
    """Извлечение status_code из response.status_code."""

    class _Response:
        status_code = 429

    class _Exc(Exception):
        response = _Response()

    assert _extract_status_code(_Exc()) == 429


def test_extract_status_code_returns_none() -> None:
    """Без status_code → None."""
    assert _extract_status_code(RuntimeError("oops")) is None


# ---------------------------------------------------------------------------
# _extract_retry_after
# ---------------------------------------------------------------------------


def test_extract_retry_after_numeric_header() -> None:
    """Retry-After: 30 → 30.0."""

    class _Response:
        headers = {"Retry-After": "30"}

    class _Exc(Exception):
        response = _Response()

    assert _extract_retry_after(_Exc()) == 30.0


def test_extract_retry_after_lowercase_header() -> None:
    """retry-after (строчные) → значение считывается."""

    class _Response:
        headers = {"retry-after": "10"}

    class _Exc(Exception):
        response = _Response()

    assert _extract_retry_after(_Exc()) == 10.0


def test_extract_retry_after_no_header() -> None:
    """Заголовок Retry-After отсутствует → None."""

    class _Response:
        headers = {}

    class _Exc(Exception):
        response = _Response()

    assert _extract_retry_after(_Exc()) is None


def test_extract_retry_after_no_response() -> None:
    """Нет response на исключении → None."""
    assert _extract_retry_after(RuntimeError("oops")) is None


def test_extract_retry_after_invalid_value() -> None:
    """Нечисловое значение Retry-After → None."""

    class _Response:
        headers = {"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}

    class _Exc(Exception):
        response = _Response()

    assert _extract_retry_after(_Exc()) is None


# ---------------------------------------------------------------------------
# chat — Retry-After header respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_respects_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 с Retry-After: 5 → задержка не меньше 5s (вместо backoff 0.01s)."""
    call_count = 0
    sleep_calls: list[float] = []

    class _Response:
        headers = {"Retry-After": "5"}

    class _HttpError(Exception):
        status_code = 429
        response = _Response()

    class _Giga:
        def chat(self, _):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _HttpError("Too many requests")
            return _MockResponse(choices=[_MockChoice(_MockMessage("ok"))])

    async def _mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("alla.clients.gigachat_client.asyncio.sleep", _mock_sleep)

    client = _make_client(_Giga())
    result = await client.chat("sys", "user")

    assert result.text == "ok"
    assert call_count == 2
    # backoff would be 0.01s, but Retry-After says 5s → delay must be >= 5s
    assert len(sleep_calls) == 1
    assert sleep_calls[0] >= 5.0


@pytest.mark.asyncio
async def test_chat_uses_backoff_when_retry_after_smaller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After меньше backoff → используется backoff."""
    call_count = 0
    sleep_calls: list[float] = []

    class _Response:
        headers = {"Retry-After": "0"}

    class _HttpError(Exception):
        status_code = 429
        response = _Response()

    class _Giga:
        def chat(self, _):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _HttpError("Too many requests")
            return _MockResponse(choices=[_MockChoice(_MockMessage("ok"))])

    async def _mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("alla.clients.gigachat_client.asyncio.sleep", _mock_sleep)

    client = _make_client(_Giga())
    result = await client.chat("sys", "user")

    assert result.text == "ok"
    # backoff=0.01 > retry_after=0 → use backoff
    assert sleep_calls[0] == pytest.approx(0.01)
