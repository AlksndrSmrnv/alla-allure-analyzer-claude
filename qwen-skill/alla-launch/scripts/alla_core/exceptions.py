"""Иерархия пользовательских исключений ядра alla."""


class AllaError(Exception):
    """Базовое исключение для всех ошибок alla."""


class ConfigurationError(AllaError):
    """Отсутствующая или некорректная конфигурация."""


class AuthenticationError(AllaError):
    """Не удалось аутентифицироваться в Allure TestOps."""


class AllureApiError(AllaError):
    """HTTP-ошибка от Allure TestOps API."""

    def __init__(self, status_code: int, message: str, endpoint: str) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        super().__init__(f"HTTP {status_code} от {endpoint}: {message}")


class PaginationLimitError(AllaError):
    """Превышен максимальный лимит страниц (защитный механизм).

    ``pages_needed`` — сколько страниц в прогоне по ответу TestOps (для подсказки).
    """

    def __init__(self, message: str, *, pages_needed: int | None = None) -> None:
        self.pages_needed = pages_needed
        super().__init__(message)
