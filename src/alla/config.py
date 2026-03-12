"""Конфигурация приложения, загружаемая из переменных окружения."""


from pydantic import Field, field_validator
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

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "ALLURE_ENDPOINT не может быть пустым. "
                "Укажите URL сервера Allure TestOps (например https://allure.company.com)"
            )
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"ALLURE_ENDPOINT должен начинаться с http:// или https://, получено: {v!r}"
            )
        return v
    project_id: int | None = Field(default=None, description="ID проекта в Allure TestOps")

    request_timeout: int = Field(default=30, description="Таймаут HTTP-запросов в секундах")
    page_size: int = Field(default=100, description="Результатов на страницу при пагинации")
    max_pages: int = Field(default=50, description="Защитный лимит на количество страниц пагинации")

    detail_concurrency: int = Field(default=10, ge=1, description="Макс. параллельных запросов при получении деталей отдельных результатов тестов")

    log_level: str = Field(default="INFO", description="Уровень логирования")
    ssl_verify: bool = Field(default=True, description="Проверка SSL-сертификатов (отключить для корпоративных прокси)")

    clustering_threshold: float = Field(default=0.60, description="Порог схожести для группировки ошибок в кластеры (0.0-1.0)")

    kb_min_score: float = Field(
        default=0.15,
        ge=0.0, le=1.0,
        description="Минимальный score для включения KB-совпадения в отчёт",
    )
    kb_max_results: int = Field(
        default=5,
        ge=1,
        description="Максимум KB-совпадений на один кластер",
    )
    kb_postgres_dsn: str = Field(
        default="",
        description=(
            "Строка подключения PostgreSQL для KB-бэкенда. "
            "Если задан — KB, feedback и KB push включаются автоматически. "
            "Пример: postgresql://user:pass@localhost:5432/alla_kb"
        ),
    )
    feedback_server_url: str = Field(
        default="",
        description=(
            "URL alla-server для API feedback из HTML-отчёта "
            "(например http://alla.company.com:8090). "
            "Если пусто — интерактивные элементы в отчёте не отображаются."
        ),
    )

    server_host: str = Field(default="0.0.0.0", description="Хост для HTTP-сервера")
    server_port: int = Field(default=8090, ge=1, le=65535, description="Порт для HTTP-сервера")

    logs_concurrency: int = Field(
        default=5, ge=1,
        description="Макс. параллельных запросов при скачивании аттачментов",
    )
    logs_clustering_weight: float = Field(
        default=0.0,
        description="Вес лог-канала в кластеризации (0.0 = не используется в кластеризации)",
    )

    langflow_base_url: str = Field(
        default="",
        description="Базовый URL Langflow API. Если задан вместе с flow_id — LLM и LLM push включаются автоматически.",
    )
    langflow_flow_id: str = Field(default="", description="ID flow в Langflow для анализа ошибок")
    langflow_api_key: str = Field(default="", description="API-ключ для аутентификации в Langflow (Bearer token)")
    llm_timeout: int = Field(default=120, ge=10, description="Таймаут одного LLM-запроса в секундах")
    llm_concurrency: int = Field(default=3, ge=1, description="Макс. параллельных запросов к Langflow API")
    llm_max_retries: int = Field(
        default=3, ge=0,
        description="Макс. число повторных попыток при 429/503/сетевых ошибках Langflow (0 = без retry)",
    )
    llm_retry_base_delay: float = Field(
        default=1.0, ge=0.1,
        description="Базовая задержка в секундах для exponential backoff (delay = base * 2^attempt)",
    )

    report_url: str = Field(
        default="",
        description="URL HTML-отчёта для прикрепления к запуску в Allure TestOps (ALLURE_REPORT_URL)",
    )
    report_link_name: str = Field(
        default="[Alla] HTML-отчёт запуска автотестов",
        description="Название ссылки HTML-отчёта в Allure TestOps (ALLURE_REPORT_LINK_NAME)",
    )

    reports_dir: str = Field(
        default="",
        description="Директория для сохранения HTML-отчётов (ALLURE_REPORTS_DIR). "
        "В Kubernetes — путь к PersistentVolume. Если пусто — отчёты не сохраняются.",
    )
    server_external_url: str = Field(
        default="",
        description="Внешний URL alla-сервера (ALLURE_SERVER_EXTERNAL_URL). "
        "Используется для ссылок на отчёты в TestOps. Пример: https://alla.company.com",
    )
    secman_ssl_verify: str = Field(
        default="true",
        description=(
            "Проверка SSL-сертификатов secman/Vault. "
            "Допустимые значения: 'true', 'false' или путь к CA bundle "
            "(например /etc/ssl/certs/ca-bundle.crt)."
        ),
    )
    secman_addr: str = Field(
        default="",
        description="URL secman/Vault API для чтения секретов через hvac.",
    )
    secman_namespace: str = Field(
        default="",
        description="Namespace secman/Vault Enterprise. Если не используется — оставить пустым.",
    )
    secman_k8s_role: str = Field(
        default="",
        description="Имя Kubernetes auth role в secman.",
    )
    secman_k8s_jwt_path: str = Field(
        default="/var/run/secrets/kubernetes.io/serviceaccount/token",
        description="Путь к service account JWT для Kubernetes auth в secman.",
    )
    secman_kv_version: str = Field(
        default="v2",
        description="Версия KV secret engine для secman helper-а. Сейчас поддерживается только v2.",
    )
    secman_mount_point: str = Field(
        default="",
        description="KV mount point в secman, например secret.",
    )
    secman_secret_path: str = Field(
        default="",
        description="Путь секрета внутри KV mount point, например alla/prod.",
    )

    @property
    def kb_active(self) -> bool:
        """KB включена автоматически если задан PostgreSQL DSN."""
        return bool(self.kb_postgres_dsn)

    @property
    def llm_active(self) -> bool:
        """LLM включён автоматически если заданы Langflow URL и flow ID."""
        return bool(self.langflow_base_url and self.langflow_flow_id)
