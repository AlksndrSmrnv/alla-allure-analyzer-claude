"""Настройки скилла alla-launch.

Ядро (``alla_core``) ожидает объект ``Settings``: доступ к TestOps, логи,
кластеризация, лимиты промптов. Значения читаются из ``<skill>/.env`` и переменных окружения
``ALLURE_*`` (окружение важнее файла). Чужие ключи игнорируются: скилл
живёт в чужом проекте, где рядом могут быть любые переменные.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from alla_core.exceptions import ConfigurationError

ENV_PREFIX = "ALLURE_"
_SECRET_FIELDS = {"token"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    """Минимальная конфигурация: доступ к TestOps, логи, кластеризация, промпты."""

    endpoint: str = ""
    token: str = field(default="", repr=False)
    request_timeout: int = 30
    # Повторы GET при 429/502/503/504 и сетевых сбоях (пауза 1, 2, 4… с или Retry-After).
    http_retries: int = 3
    page_size: int = 100
    # 200 страниц по 100 — 20 000 результатов вместе со скрытыми повторами.
    max_pages: int = 200
    detail_concurrency: int = 10
    ssl_verify: bool = True
    logs_concurrency: int = 5
    logs_max_attachment_bytes: int = 10 * 1024 * 1024
    logs_max_snippet_chars: int = 64 * 1024
    clustering_threshold: float = 0.60
    logs_clustering_weight: float = 0.15
    clustering_step_strict_threshold: float = 0.95
    clustering_log_split_threshold: float = 0.30
    clustering_resource_gate: bool = True
    llm_prompt_message_max_chars: int = 2000
    llm_prompt_trace_max_chars: int = 400
    llm_prompt_log_max_chars: int = 8000
    retry_max_detail_requests: int = 200
    images_per_problem: int = 3
    images_max_bytes: int = 5 * 1024 * 1024
    # Ключи id операции проекта через запятую (``ufsBusinessId,ufsRqUID``) — к реестру
    # ``log_utils._CANONICAL_CORRELATION_KEYS``: по ним из общего лога берутся записи операции.
    operation_id_keys: str = ""

    @classmethod
    def load(
        cls,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Собрать настройки из ``env_file`` и окружения и проверить обязательные."""
        values: dict[str, str] = {}
        if env_file is not None and env_file.is_file():
            values.update(read_env_file(env_file))
        values.update(os.environ if environ is None else environ)

        kwargs: dict[str, object] = {}
        for item in dataclasses.fields(cls):
            raw = values.get(ENV_PREFIX + item.name.upper())
            if raw is None:
                continue
            kwargs[item.name] = _coerce(item.name, type(item.default), raw.strip())
        settings = cls(**kwargs)  # type: ignore[arg-type]
        settings.validate()
        return settings

    @property
    def operation_keys(self) -> tuple[str, ...]:
        """Ключи ``operation_id_keys`` без пустых и повторов (регистр не важен), как написаны."""
        keys: dict[str, str] = {}
        for raw in self.operation_id_keys.split(","):
            key = raw.strip()
            if key:
                keys.setdefault(key.lower(), key)
        return tuple(keys.values())

    def validate(self) -> None:
        endpoint = self.endpoint.strip()
        if not endpoint:
            raise ConfigurationError(
                "Не задан ALLURE_ENDPOINT — URL сервера Allure TestOps "
                "(например https://allure.company.com)"
            )
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigurationError(
                f"ALLURE_ENDPOINT должен начинаться с http:// или https://, получено: {endpoint!r}"
            )
        if not self.token.strip():
            raise ConfigurationError("Не задан ALLURE_TOKEN — API-токен Allure TestOps")
        for key in self.operation_keys:
            if not _OPERATION_KEY_RE.fullmatch(key):
                raise ConfigurationError(
                    f"{ENV_PREFIX}OPERATION_ID_KEYS: «{key}» — не имя поля; "
                    "ключи через запятую, например ufsBusinessId,ufsRqUID"
                )
        for name, (low, high) in BOUNDS.items():
            value = getattr(self, name)
            if (low is not None and value < low) or (high is not None and value > high):
                allowed = f"от {low}" if high is None else f"от {low} до {high}"
                raise ConfigurationError(
                    f"{ENV_PREFIX}{name.upper()}: допустимо {allowed}, получено {value}"
                )


_OPERATION_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")

# Допустимые границы (ge, le) значений настроек.
# Например, параллелизм 0 навсегда подвесит asyncio.Semaphore в триаже и логах.
BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "http_retries": (0, 10),
    "page_size": (1, None),
    "max_pages": (1, None),
    "request_timeout": (1, None),
    "detail_concurrency": (1, None),
    "logs_concurrency": (1, None),
    "logs_max_attachment_bytes": (1024, None),
    "logs_max_snippet_chars": (1000, None),
    "clustering_threshold": (0.0, 1.0),
    "logs_clustering_weight": (0.0, 1.0),
    "clustering_step_strict_threshold": (0.0, 1.0),
    "clustering_log_split_threshold": (0.0, 1.0),
    "llm_prompt_message_max_chars": (100, None),
    "llm_prompt_trace_max_chars": (50, None),
    "llm_prompt_log_max_chars": (100, None),
    "retry_max_detail_requests": (0, None),
    "images_per_problem": (0, 3),
    "images_max_bytes": (64 * 1024, 20 * 1024 * 1024),
}


def read_env_file(path: Path) -> dict[str, str]:
    """Прочитать простой ``.env``: ``KEY=VALUE``, ``export KEY=VALUE``, комментарии ``#``."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _coerce(name: str, kind: type, raw: str) -> object:
    env_name = ENV_PREFIX + name.upper()
    shown = "<скрыто>" if name in _SECRET_FIELDS else repr(raw)
    if kind is str:
        return raw
    if kind is bool:
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ConfigurationError(f"{env_name}: ожидается true/false, получено {shown}")
    try:
        return kind(raw)
    except ValueError:
        raise ConfigurationError(
            f"{env_name}: ожидается число, получено {shown}"
        ) from None
