"""Клиент GigaChat для LLM-анализа кластеров ошибок."""

import asyncio
import logging
from dataclasses import dataclass

from alla.exceptions import LLMApiError
from alla.models.llm import TokenUsage

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Ответ GigaChat: текст + статистика токенов."""

    text: str
    token_usage: TokenUsage = TokenUsage()


class _FallbackMessagesRole:
    """Минимальный fallback для тестов без установленного gigachat.models."""

    SYSTEM = "system"
    USER = "user"


@dataclass(slots=True)
class _FallbackMessage:
    role: str
    content: str


@dataclass(slots=True)
class _FallbackChat:
    messages: list[_FallbackMessage]
    model: str
    stream: bool
    update_interval: int


def _build_chat_request(system_prompt: str, user_prompt: str, model: str) -> object:
    """Собрать Chat-request для GigaChat SDK.

    В тестовом окружении без пакета ``gigachat`` используем лёгкий fallback,
    чтобы можно было проверить retry/parsing логику клиента без установки SDK.
    """
    try:
        from gigachat.models import Chat, Messages, MessagesRole
    except ModuleNotFoundError:
        Chat = _FallbackChat
        Messages = _FallbackMessage
        MessagesRole = _FallbackMessagesRole

    return Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system_prompt),
            Messages(role=MessagesRole.USER, content=user_prompt),
        ],
        model=model,
        stream=False,
        update_interval=0,
    )


def _extract_token_usage(response: object) -> TokenUsage:
    """Извлечь статистику токенов из ответа GigaChat SDK.

    Если ``response.usage`` отсутствует или ``None`` — возвращает нули.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    try:
        return TokenUsage(
            prompt_tokens=int(usage.prompt_tokens),
            completion_tokens=int(usage.completion_tokens),
            total_tokens=int(usage.total_tokens),
        )
    except (AttributeError, TypeError, ValueError):
        return TokenUsage()


class GigaChatClient:
    """Асинхронная обёртка над синхронным GigaChat SDK.

    Отправляет system + user сообщения в GigaChat и возвращает
    текстовый ответ.  Поддерживает retry с exponential backoff
    при сетевых ошибках и 429 / 502-504.
    """

    def __init__(
        self,
        base_url: str,
        cert_file: str,
        key_file: str,
        *,
        model: str = "GigaChat-2-Max",
        verify_ssl: bool = True,
        timeout: int = 120,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        from gigachat import GigaChat

        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._giga = GigaChat(
            base_url=base_url,
            cert_file=cert_file,
            key_file=key_file,
            verify_ssl_certs=verify_ssl,
            model=model,
            timeout=timeout,
        )

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse:
        """Отправить system + user сообщения в GigaChat и вернуть ответ.

        Retry: при сетевых ошибках и 429/502-504 — exponential backoff
        (delay = base * 2^attempt), до ``max_retries`` повторов.
        """
        chat_request = _build_chat_request(system_prompt, user_prompt, self._model)

        last_error: LLMApiError | None = None

        for attempt in range(1 + self._max_retries):
            logger.debug(
                "GigaChat запрос: model=%s, user_len=%d, attempt=%d/%d",
                self._model,
                len(user_prompt),
                attempt + 1,
                1 + self._max_retries,
            )

            retryable = False
            try:
                response = await asyncio.to_thread(self._giga.chat, chat_request)
            except Exception as exc:
                exc_str = str(exc)
                status_code = _extract_status_code(exc)

                if status_code and status_code >= 400 and status_code not in _RETRYABLE_STATUS_CODES:
                    raise LLMApiError(status_code, exc_str, self._base_url) from exc

                last_error = LLMApiError(status_code or 0, exc_str, self._base_url)
                last_error.__cause__ = exc
                retryable = True
            else:
                try:
                    text = response.choices[0].message.content
                except (IndexError, AttributeError) as exc:
                    raise LLMApiError(
                        0,
                        f"Неожиданная структура ответа GigaChat: {exc}. "
                        f"Ответ: {str(response)[:300]}",
                        self._base_url,
                    ) from exc

                if not isinstance(text, str):
                    raise LLMApiError(
                        0,
                        f"Ожидался str, получен {type(text).__name__}",
                        self._base_url,
                    )
                return ChatResponse(text=text, token_usage=_extract_token_usage(response))

            if retryable and attempt < self._max_retries:
                delay = self._retry_base_delay * (2 ** attempt)
                logger.warning(
                    "GigaChat ошибка (попытка %d/%d): %s — повтор через %.1fs",
                    attempt + 1,
                    1 + self._max_retries,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)
            elif last_error is not None:
                raise last_error

        raise last_error  # type: ignore[misc]

    async def close(self) -> None:
        """Освободить ресурсы."""
        close = getattr(self._giga, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    async def __aenter__(self) -> "GigaChatClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


def _extract_status_code(exc: Exception) -> int | None:
    """Попытаться извлечь HTTP status code из исключения GigaChat SDK."""
    if hasattr(exc, "status_code"):
        return int(exc.status_code)
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        return int(exc.response.status_code)
    return None
