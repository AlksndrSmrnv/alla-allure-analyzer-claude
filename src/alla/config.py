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
    project_id: int | None = Field(default=None, description="ID проекта в Allure TestOps")

    request_timeout: int = Field(default=30, description="Таймаут HTTP-запросов в секундах")
    page_size: int = Field(default=100, description="Результатов на страницу при пагинации")
    max_pages: int = Field(default=50, description="Защитный лимит на количество страниц пагинации")

    detail_concurrency: int = Field(default=10, ge=1, description="Макс. параллельных запросов при получении деталей отдельных результатов тестов")

    log_level: str = Field(default="INFO", description="Уровень логирования")
    ssl_verify: bool = Field(default=True, description="Проверка SSL-сертификатов (отключить для корпоративных прокси)")

    clustering_enabled: bool = Field(default=True, description="Включить/выключить кластеризацию ошибок")
    clustering_threshold: float = Field(default=0.60, description="Порог схожести для группировки ошибок в кластеры (0.0-1.0)")

    kb_enabled: bool = Field(default=False, description="Включить/выключить поиск по базе знаний")
    kb_path: str = Field(default="knowledge_base", description="Путь к директории с YAML-файлами базы знаний")
    kb_push_enabled: bool = Field(
        default=False,
        description="Записывать рекомендации KB обратно в Allure TestOps через комментарии к тест-кейсам",
    )

    server_host: str = Field(default="0.0.0.0", description="Хост для HTTP-сервера")
    server_port: int = Field(default=8090, ge=1, le=65535, description="Порт для HTTP-сервера")

    logs_enabled: bool = Field(default=False, description="Включить/выключить извлечение и анализ логов из аттачментов")
    logs_max_size_kb: int = Field(
        default=512, ge=1,
        description="Максимальный размер лог-сниппета на один тест (КБ)",
    )
    logs_concurrency: int = Field(
        default=5, ge=1,
        description="Макс. параллельных запросов при скачивании аттачментов",
    )
    logs_clustering_weight: float = Field(
        default=0.0,
        description="Вес лог-канала в кластеризации (0.0 = не используется в кластеризации)",
    )

    llm_enabled: bool = Field(default=False, description="Включить/выключить LLM-анализ кластеров через Langflow")
    langflow_base_url: str = Field(default="", description="Базовый URL Langflow API (например http://langflow.company.com)")
    langflow_flow_id: str = Field(default="", description="ID flow в Langflow для анализа ошибок")
    langflow_api_key: str = Field(default="", description="API-ключ для аутентификации в Langflow (Bearer token)")
    llm_timeout: int = Field(default=120, ge=10, description="Таймаут одного LLM-запроса в секундах")
    llm_concurrency: int = Field(default=3, ge=1, description="Макс. параллельных запросов к Langflow API")
    llm_push_enabled: bool = Field(
        default=False,
        description="Записывать результаты LLM-анализа в Allure TestOps через комментарии к тест-кейсам",
    )
