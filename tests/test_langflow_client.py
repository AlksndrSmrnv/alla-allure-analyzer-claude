"""Тесты HTTP-клиента LangflowClient: запросы, ошибки, парсинг ответов."""

from __future__ import annotations

import httpx
import pytest

from alla.clients.langflow_client import LangflowClient
from alla.exceptions import LangflowApiError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_langflow_response(text: str = "LLM analysis result") -> dict:
    """Стандартный JSON-ответ Langflow."""
    return {
        "outputs": [{
            "outputs": [{
                "results": {
                    "message": {
                        "text": text,
                    },
                },
            }],
        }],
    }


class _MockResponse:
    """Заглушка для httpx.Response."""

    def __init__(
        self,
        json_data: dict | None = None,
        status_code: int = 200,
        text: str = "",
        *,
        invalid_json: bool = False,
    ) -> None:
        self._json_data = json_data
        self.status_code = status_code
        self.text = text
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("Invalid JSON")
        return self._json_data


def _make_client(
    mock_http,
    *,
    api_key: str = "test-key",
) -> LangflowClient:
    """Создать LangflowClient с подменённым HTTP-клиентом."""
    client = LangflowClient(
        base_url="https://langflow.test",
        flow_id="flow-123",
        api_key=api_key,
    )
    client._http = mock_http
    return client


# ---------------------------------------------------------------------------
# run_flow — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_flow_success() -> None:
    """Успешный запрос: текст извлечён из JSON-ответа."""

    class _Http:
        async def post(self, url, json, headers):
            return _MockResponse(_valid_langflow_response("Hello LLM"))

    client = _make_client(_Http())
    result = await client.run_flow("test input")

    assert result == "Hello LLM"


@pytest.mark.asyncio
async def test_run_flow_sends_correct_url_and_payload() -> None:
    """URL содержит flow_id, payload содержит input_value и типы."""
    captured_url = None
    captured_json = None

    class _Http:
        async def post(self, url, json, headers):
            nonlocal captured_url, captured_json
            captured_url = url
            captured_json = json
            return _MockResponse(_valid_langflow_response())

    client = _make_client(_Http())
    await client.run_flow("my prompt")

    assert captured_url == "https://langflow.test/langflow/api/v1/run/flow-123"
    assert captured_json == {
        "input_value": "my prompt",
        "output_type": "chat",
        "input_type": "chat",
    }


@pytest.mark.asyncio
async def test_run_flow_includes_api_key_header() -> None:
    """Непустой api_key → заголовок x-api-key присутствует."""
    captured_headers = None

    class _Http:
        async def post(self, url, json, headers):
            nonlocal captured_headers
            captured_headers = headers
            return _MockResponse(_valid_langflow_response())

    client = _make_client(_Http(), api_key="secret-key")
    await client.run_flow("test")

    assert captured_headers["x-api-key"] == "secret-key"


@pytest.mark.asyncio
async def test_run_flow_omits_api_key_when_empty() -> None:
    """Пустой api_key → заголовок x-api-key отсутствует."""
    captured_headers = None

    class _Http:
        async def post(self, url, json, headers):
            nonlocal captured_headers
            captured_headers = headers
            return _MockResponse(_valid_langflow_response())

    client = _make_client(_Http(), api_key="")
    await client.run_flow("test")

    assert "x-api-key" not in captured_headers


# ---------------------------------------------------------------------------
# run_flow — HTTP ошибки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_flow_timeout_raises_langflow_error() -> None:
    """TimeoutException → LangflowApiError с status_code=0."""

    class _Http:
        async def post(self, url, json, headers):
            raise httpx.ReadTimeout("timed out")

    client = _make_client(_Http())

    with pytest.raises(LangflowApiError) as exc_info:
        await client.run_flow("test")

    assert exc_info.value.status_code == 0
    assert "Таймаут" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_flow_network_error_raises_langflow_error() -> None:
    """RequestError (сеть) → LangflowApiError с status_code=0."""

    class _Http:
        async def post(self, url, json, headers):
            raise httpx.ConnectError("Connection refused")

    client = _make_client(_Http())

    with pytest.raises(LangflowApiError) as exc_info:
        await client.run_flow("test")

    assert exc_info.value.status_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 500, 502])
async def test_run_flow_http_error_raises_langflow_error(status_code: int) -> None:
    """HTTP >=400 → LangflowApiError с правильным status_code."""

    class _Http:
        async def post(self, url, json, headers):
            return _MockResponse(status_code=status_code, text="error body")

    client = _make_client(_Http())

    with pytest.raises(LangflowApiError) as exc_info:
        await client.run_flow("test")

    assert exc_info.value.status_code == status_code


# ---------------------------------------------------------------------------
# run_flow — ошибки парсинга
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_flow_invalid_json_raises_langflow_error() -> None:
    """Ответ не JSON → LangflowApiError."""

    class _Http:
        async def post(self, url, json, headers):
            return _MockResponse(invalid_json=True, text="<html>not json</html>")

    client = _make_client(_Http())

    with pytest.raises(LangflowApiError, match="не является валидным JSON"):
        await client.run_flow("test")


# ---------------------------------------------------------------------------
# _extract_text — ошибки структуры
# ---------------------------------------------------------------------------


def test_extract_text_missing_outputs_key() -> None:
    """JSON без ключа 'outputs' → LangflowApiError."""
    with pytest.raises(LangflowApiError, match="Неожиданная структура"):
        LangflowClient._extract_text({"data": []}, "http://test")


def test_extract_text_empty_outputs_list() -> None:
    """outputs=[] (пустой список) → LangflowApiError."""
    with pytest.raises(LangflowApiError, match="Неожиданная структура"):
        LangflowClient._extract_text({"outputs": []}, "http://test")


def test_extract_text_missing_nested_key() -> None:
    """Отсутствует 'results' во вложенности → LangflowApiError."""
    data = {"outputs": [{"outputs": [{"no_results": {}}]}]}
    with pytest.raises(LangflowApiError, match="Неожиданная структура"):
        LangflowClient._extract_text(data, "http://test")


def test_extract_text_text_not_string() -> None:
    """text=123 (не str) → LangflowApiError."""
    data = {"outputs": [{"outputs": [{"results": {"message": {"text": 123}}}]}]}
    with pytest.raises(LangflowApiError, match="Неожиданная структура"):
        LangflowClient._extract_text(data, "http://test")


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_calls_close() -> None:
    """async with LangflowClient → close() вызывается при выходе."""
    close_called = False

    class _Http:
        async def aclose(self):
            nonlocal close_called
            close_called = True

    client = LangflowClient(
        base_url="https://test",
        flow_id="f1",
        api_key="key",
    )
    client._http = _Http()

    async with client:
        pass

    assert close_called


# ---------------------------------------------------------------------------
# Конструктор
# ---------------------------------------------------------------------------


def test_constructor_strips_trailing_slash() -> None:
    """base_url с trailing slash → внутри без slash."""
    client = LangflowClient(
        base_url="https://langflow.test/",
        flow_id="f1",
        api_key="key",
    )
    assert client._base_url == "https://langflow.test"
