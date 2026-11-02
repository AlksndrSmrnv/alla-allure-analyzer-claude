"""Подсказки пользователю по типовым сбоям получения прогона из TestOps.

Клиент MCP оборачивает сетевые ошибки в ``AllureApiError(0, …)``, ответы MCP не по
протоколу — в ``McpError``. Здесь по цепочке причин подбирается конкретное действие:
какую переменную ``.env`` поправить.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx

from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, AuthenticationError, McpError, PaginationLimitError


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


# Настройки и .env меняет пользователь: модель, получив «задай ALLURE_ENDPOINT=…»,
# иначе сама правила бы файл с токеном.
USER_ACTION_NOTE = (
    "Это настройка пользователя: скажи ему, что нужно сделать, и остановись. "
    "Не меняй .env и переменные окружения сам, содержимое .env не читай и не выводи."
)


def fetch_error_hint(exc: BaseException, settings: Settings, env_file: Path) -> str:
    """Что должен сделать пользователь; пустая строка, если причина неизвестна."""
    causes = list(_chain(exc))
    texts = " ".join(str(item) for item in causes)
    if any(isinstance(item, ssl.SSLError) for item in causes) or "CERTIFICATE_VERIFY_FAILED" in texts:
        # Проверку TLS скилл отключать не умеет: доверие корпоративному прокси задаётся его
        # корневым сертификатом. httpx берёт SSL_CERT_FILE только из окружения.
        return (
            "Не удалось проверить TLS-сертификат. Если сертификат подменяет корпоративный "
            "прокси, укажи файл с его корневым сертификатом в переменной окружения "
            "SSL_CERT_FILE (в .env скилла она не читается; файл заменяет встроенный список "
            "доверенных сертификатов) и перезапусти Qwen Code. Проверку сертификата "
            "отключить нельзя: иначе API-токен смог бы перехватить посредник."
        )
    if any(isinstance(item, httpx.TimeoutException) for item in causes):
        return (
            f"Таймаут запроса к TestOps. Увеличь ALLURE_REQUEST_TIMEOUT "
            f"(сейчас {settings.request_timeout} с) в {env_file}."
        )
    if any(isinstance(item, httpx.ConnectError) for item in causes):
        return (
            f"Нет соединения с {settings.endpoint}. Проверь ALLURE_ENDPOINT, VPN и прокси."
        )
    limit = next((item for item in causes if isinstance(item, PaginationLimitError)), None)
    if limit is not None:
        needed = (
            f" хотя бы до {limit.pages_needed}" if limit.pages_needed else ""
        )
        return (
            f"Результатов больше лимита страниц. Увеличь ALLURE_MAX_PAGES{needed} "
            f"(сейчас {settings.max_pages}) или ALLURE_PAGE_SIZE в {env_file}."
        )
    mcp = next((item for item in causes if isinstance(item, McpError)), None)
    if mcp is not None and mcp.reason in ("not_found", "no_tool"):
        return (
            f"MCP-сервер TestOps не найден по адресу {settings.mcp_endpoint} или в нём нет "
            "поиска результатов. Нужен TestOps 26.2.1.8 или новее с включённым MCP; если "
            f"сервер по другому адресу, задай ALLURE_MCP_URL в {env_file}."
        )
    if mcp is not None and mcp.reason == "transport":
        return (
            f"Сервер {settings.mcp_endpoint} не принимает Streamable HTTP. Проверь "
            f"ALLURE_MCP_URL в {env_file}: нужен адрес встроенного MCP-сервера TestOps "
            "(<адрес TestOps>/api/mcp)."
        )
    api = next((item for item in causes if isinstance(item, AllureApiError)), None)
    if api is not None and api.status_code == 404:
        return (
            "Запуск не найден: проверь номер запуска и что ALLURE_ENDPOINT указывает "
            "на нужный TestOps (адрес сайта, без /api в конце)."
        )
    if (
        any(isinstance(item, AuthenticationError) for item in causes)
        or (api is not None and api.status_code in (401, 403))
    ):
        return (
            f"Проверь ALLURE_TOKEN в {env_file} (или в переменных окружения): токен "
            "неверный, просрочен или у него нет доступа к проекту."
        )
    return ""
