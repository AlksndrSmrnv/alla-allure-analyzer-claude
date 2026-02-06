"""Конфигурация приложения, загружаемая из переменных окружения."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация приложения alla.

    Все значения задаются через переменные окружения с префиксом ``ALLURE_``
    или через файл ``.env`` в рабочей директории.
    """

    model_config = SettingsConfigDict(
        env_prefix="ALLURE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    endpoint: str = Field(description="URL сервера Allure TestOps")
    token: str = Field(description="API-токен для аутентификации")
    project_id: int = Field(description="ID проекта в Allure TestOps")

    request_timeout: int = Field(default=30, description="Таймаут HTTP-запросов в секундах")
    page_size: int = Field(default=100, description="Результатов на страницу при пагинации")
    max_pages: int = Field(default=50, description="Защитный лимит на количество страниц пагинации")

    detail_concurrency: int = Field(default=10, description="Макс. параллельных запросов при получении деталей отдельных результатов тестов")

    log_level: str = Field(default="INFO", description="Уровень логирования")
    ssl_verify: bool = Field(default=True, description="Проверка SSL-сертификатов (отключить для корпоративных прокси)")

    clustering_enabled: bool = Field(default=True, description="Включить/выключить кластеризацию ошибок")
    clustering_threshold: float = Field(default=0.60, description="Порог схожести для группировки ошибок в кластеры (0.0-1.0)")
