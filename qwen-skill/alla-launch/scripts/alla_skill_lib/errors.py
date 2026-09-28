"""Подсказки пользователю по типовым сбоям получения прогона из TestOps.

Вендоренный клиент оборачивает сетевые ошибки в ``AllureApiError(0, …)`` и
пишет для 404 общее «проверьте версию TestOps». Здесь по цепочке причин
подбирается конкретное действие: какую переменную ``.env`` поправить.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx

from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, AuthenticationError, PaginationLimitError


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def fetch_error_hint(exc: BaseException, settings: Settings, env_file: Path) -> str:
    """Что сделать пользователю; пустая строка, если причина неизвестна."""
    causes = list(_chain(exc))
    texts = " ".join(str(item) for item in causes)
    if any(isinstance(item, ssl.SSLError) for item in causes) or "CERTIFICATE_VERIFY_FAILED" in texts:
        return (
            "Не удалось проверить TLS-сертификат. Для корпоративного прокси с "
            "самоподписанным сертификатом задай ALLURE_SSL_VERIFY=false "
            f"(в {env_file} или в переменных окружения)."
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
    if any(isinstance(item, PaginationLimitError) for item in causes):
        return (
            f"Результатов больше лимита страниц. Увеличь ALLURE_MAX_PAGES "
            f"(сейчас {settings.max_pages}) или ALLURE_PAGE_SIZE в {env_file}."
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
