"""Настройки скилла alla-launch (рукописный shim вместо ``alla/config.py``).

Вендоренный код ядра ожидает объект ``Settings`` с полями серверной
конфигурации. Здесь только те поля, которые нужны скиллу, с дефолтами как
у сервера. Значения читаются из ``<skill>/.env`` и переменных окружения
``ALLURE_*`` (окружение важнее файла). Чужие ключи игнорируются: скилл
живёт в чужом проекте, где рядом могут быть любые переменные.
"""

from __future__ import annotations

import dataclasses
import os
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
    page_size: int = 100
    max_pages: int = 50
    detail_concurrency: int = 10
    ssl_verify: bool = True
    logs_concurrency: int = 5
    logs_max_attachment_bytes: int = 10 * 1024 * 1024
    logs_max_snippet_chars: int = 64 * 1024
    clustering_threshold: float = 0.60
    logs_clustering_weight: float = 0.15
    clustering_step_strict_threshold: float = 0.95
    llm_prompt_message_max_chars: int = 2000
    llm_prompt_trace_max_chars: int = 400
    llm_prompt_log_max_chars: int = 8000

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
