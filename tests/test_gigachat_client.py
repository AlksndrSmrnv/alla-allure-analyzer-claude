"""Тесты клиента GigaChatClient: запросы, ошибки, retry, парсинг ответов."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from alla.clients.gigachat_client import GigaChatClient, _extract_status_code
from alla.exceptions import LLMApiError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _MockMessage:
    content: str = "LLM analysis result"


@dataclass
class _MockChoice:
    message: _MockMessage = field(default_factory=_MockMessage)


@dataclass
class _MockResponse:
    choices: list[_MockChoice] = field(default_factory=lambda: [_MockChoice()])


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

    assert result == "Hello LLM"
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

    assert result == "ok"
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
