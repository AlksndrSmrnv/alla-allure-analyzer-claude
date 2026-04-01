"""Конфигурация приложения, загружаемая из переменных окружения."""

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("alla.config")


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

    vault_url: str = Field(
        default="",
        description="Полный URL Vault Proxy для получения секретов "
        "(например http://vault-proxy/v1/secret/data/alla). "
        "Если задан — ALLURE_TOKEN, ALLURE_KB_POSTGRES_DSN, ALLURE_GIGACHAT_CERT_B64, "
        "ALLURE_GIGACHAT_KEY_B64 загружаются из Vault с fallback на env vars.",
    )

    endpoint: str = Field(description="URL сервера Allure TestOps")
    token: str = Field(default="", description="API-токен для аутентификации")

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

    clustering_threshold: float = Field(
        default=0.60,
        ge=0.0, le=1.0,
        description="Порог схожести для группировки ошибок в кластеры (0.0-1.0)",
    )

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
        default=0.15,
        ge=0.0, le=1.0,
        description="Вес лог-канала в кластеризации. Лог участвует в сравнении когда доступен; при отсутствии лога вес перераспределяется на message",
    )

    gigachat_base_url: str = Field(
        default="",
        description="Базовый URL GigaChat API. Если задан вместе с cert и key — LLM включается автоматически.",
    )
    gigachat_cert_b64: str = Field(
        default="",
        description="Клиентский сертификат PEM в base64 для mTLS-аутентификации в GigaChat.",
    )
    gigachat_key_b64: str = Field(
        default="",
        description="Приватный ключ PEM в base64 для mTLS-аутентификации в GigaChat.",
    )
    gigachat_model: str = Field(
        default="GigaChat-2-Max",
        description="Название модели GigaChat (ALLURE_GIGACHAT_MODEL).",
    )
    llm_timeout: int = Field(default=120, ge=10, description="Таймаут одного LLM-запроса в секундах")
    llm_concurrency: int = Field(default=3, ge=1, description="Макс. параллельных запросов к GigaChat API")
    llm_max_retries: int = Field(
        default=3, ge=0,
        description="Макс. число повторных попыток при 429/503/сетевых ошибках GigaChat (0 = без retry)",
    )
    llm_retry_base_delay: float = Field(
        default=1.0, ge=0.1,
        description="Базовая задержка в секундах для exponential backoff (delay = base * 2^attempt)",
    )
    llm_prompt_message_max_chars: int = Field(
        default=2000,
        ge=100,
        description="Макс. символов сообщения об ошибке в LLM-промпте (ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS)",
    )
    llm_prompt_trace_max_chars: int = Field(
        default=400,
        ge=50,
        description="Макс. символов стек-трейса в LLM-промпте (ALLURE_LLM_PROMPT_TRACE_MAX_CHARS)",
    )
    llm_prompt_log_max_chars: int = Field(
        default=8000,
        ge=100,
        description="Макс. символов лога в LLM-промпте (ALLURE_LLM_PROMPT_LOG_MAX_CHARS)",
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
    reports_postgres: bool = Field(
        default=False,
        description="Сохранять HTML-отчёты в PostgreSQL (ALLURE_REPORTS_POSTGRES). "
        "Требует ALLURE_KB_POSTGRES_DSN. Таблица alla.report создаётся автоматически.",
    )
    server_external_url: str = Field(
        default="",
        description="Внешний URL alla-сервера (ALLURE_SERVER_EXTERNAL_URL). "
        "Используется для ссылок на отчёты в TestOps. Пример: https://alla.company.com",
    )

    push_to_testops: bool = Field(
        default=True,
        description="Записывать результаты анализа (KB/LLM) в Allure TestOps "
        "как комментарии к тест-кейсам (ALLURE_PUSH_TO_TESTOPS).",
    )

    @property
    def kb_active(self) -> bool:
        """KB включена автоматически если задан PostgreSQL DSN."""
        return bool(self.kb_postgres_dsn)

    @property
    def llm_active(self) -> bool:
        """LLM включён автоматически если заданы GigaChat URL, сертификат и ключ."""
        return bool(self.gigachat_base_url and self.gigachat_cert_b64 and self.gigachat_key_b64)

    # -- Vault Proxy integration --

    _VAULT_SECRET_FIELDS: dict[str, str] = {
        "ALLURE_TOKEN": "token",
        "ALLURE_KB_POSTGRES_DSN": "kb_postgres_dsn",
        "ALLURE_GIGACHAT_CERT_B64": "gigachat_cert_b64",
        "ALLURE_GIGACHAT_KEY_B64": "gigachat_key_b64",
    }

    def resolve_cert_files(self) -> tuple[str, str]:
        """Декодировать base64 сертификат и ключ GigaChat во временные файлы.

        Возвращает ``(cert_path, key_path)``.  Файлы создаются с
        ``delete=False`` — вызывающий код отвечает за их удаление.
        """
        import base64
        import tempfile

        cert_data = base64.b64decode(self.gigachat_cert_b64)
        key_data = base64.b64decode(self.gigachat_key_b64)

        cert_file = tempfile.NamedTemporaryFile(
            suffix=".pem", prefix="alla_cert_", delete=False,
        )
        cert_file.write(cert_data)
        cert_file.close()

        key_file = tempfile.NamedTemporaryFile(
            suffix=".pem", prefix="alla_key_", delete=False,
        )
        key_file.write(key_data)
        key_file.close()

        logger.debug(
            "GigaChat cert/key записаны во временные файлы: %s, %s",
            cert_file.name,
            key_file.name,
        )
        return cert_file.name, key_file.name

    def resolve_secrets(self) -> None:
        """Загрузить секреты из Vault Proxy, если ``ALLURE_VAULT_URL`` задан.

        При ошибке соединения — warning в лог, значения из env vars сохраняются.
        """
        if not self.vault_url:
            return

        import httpx

        try:
            resp = httpx.get(self.vault_url, timeout=5)
            resp.raise_for_status()
            data: dict[str, str] = resp.json().get("data", {})
        except Exception as exc:
            logger.warning("Vault Proxy недоступен (%s), используем env vars", exc)
            return

        resolved: list[str] = []
        for vault_key, field_name in self._VAULT_SECRET_FIELDS.items():
            value = data.get(vault_key)
            if value:
                object.__setattr__(self, field_name, value)
                resolved.append(vault_key)

        if resolved:
            logger.info("Секреты загружены из Vault: %s", ", ".join(resolved))
        else:
            logger.warning("Vault ответил, но не содержит ожидаемых ключей")

    def validate_required(self) -> None:
        """Проверить обязательные поля после :meth:`resolve_secrets`."""
        from alla.exceptions import ConfigurationError

        if not self.token:
            raise ConfigurationError(
                "ALLURE_TOKEN не задан. "
                "Укажите через env var или Vault Proxy (ALLURE_VAULT_URL)."
            )
