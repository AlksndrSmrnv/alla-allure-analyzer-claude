"""Утилиты для тонких скрипт-обёрток alla-skill.

Все скрипты в каталоге импортируют отсюда:

* :func:`load_settings` — :class:`alla.config.Settings`, явно загружаемый
  из ``alla-skill/.env`` (не из CWD).
* :func:`open_testops_client` — async context manager для
  :class:`alla.clients.testops_client.AllureTestOpsClient`.
* :func:`get_pg_dsn` — DSN PostgreSQL.
* :func:`print_json` / :func:`error_envelope` / :func:`exit_with_error` —
  единый формат stdout/exit code.
* :func:`parse_stdin_json` — для submit_analysis.

Скрипты — тонкие orchestration-entrypoints; вся бизнес-логика живёт в
``alla.services.*``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import sys
from typing import Any, AsyncIterator

logger = logging.getLogger("alla.skill")

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = SKILL_DIR / ".env"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 4
EXIT_CONFIG = 5


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def load_settings() -> Any:
    """Загрузить :class:`alla.config.Settings` из ``alla-skill/.env``.

    PostgreSQL обязателен для скилл-режима — все артефакты pipeline
    хранятся в ``alla.skill_run`` и ``alla.report``. Без DSN — exit 5.
    """
    from alla.config import Settings  # local import чтобы не тянуть в момент help'а
    from alla.exceptions import ConfigurationError

    if not ENV_PATH.exists():
        raise ConfigurationError(
            f".env не найден по пути {ENV_PATH}. Скопируй .env.example и заполни."
        )

    settings = Settings(_env_file=str(ENV_PATH))
    settings.resolve_secrets()
    settings.validate_required()
    if not settings.kb_active:
        raise ConfigurationError(
            "ALLURE_KB_POSTGRES_DSN обязателен для skill-режима — "
            "все артефакты pipeline хранятся в PostgreSQL."
        )
    _configure_logging(settings.log_level)
    return settings


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# TestOps client lifecycle
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def open_testops_client(settings: Any) -> AsyncIterator[Any]:
    """Открыть и корректно закрыть :class:`AllureTestOpsClient`.

    Внутри: :class:`AllureAuthManager` для JWT exchange + сам клиент.
    """
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient

    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )
    client = AllureTestOpsClient(settings, auth)
    try:
        yield client
    finally:
        try:
            await client.close()
        except Exception as exc:  # pragma: no cover
            logger.warning("Не удалось закрыть TestOps client: %s", exc)
        try:
            await auth.close()
        except Exception as exc:  # pragma: no cover
            logger.warning("Не удалось закрыть AuthManager: %s", exc)


def get_pg_dsn(settings: Any) -> str:
    """DSN PostgreSQL — короткий accessor."""
    return settings.kb_postgres_dsn


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------


def print_json(payload: Any) -> None:
    """Вывести payload на stdout как индексированный JSON."""
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _json_default(obj: Any) -> Any:
    # datetime / date / TokenUsage и пр. — превращаем в строки.
    from dataclasses import asdict, is_dataclass
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)


def error_envelope(error: str, **extra: Any) -> dict[str, Any]:
    """Стандартный envelope для ошибок."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return payload


def exit_with_error(envelope: dict[str, Any], code: int = EXIT_ERROR) -> None:
    """Распечатать envelope в stderr (как JSON) и завершиться с кодом."""
    sys.stderr.write(
        json.dumps(envelope, ensure_ascii=False, indent=2, default=_json_default)
    )
    sys.stderr.write("\n")
    sys.stderr.flush()
    sys.exit(code)


def parse_stdin_json() -> Any:
    """Прочитать JSON с stdin (для ``submit_analysis --input -``)."""
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin пустой — нечего парсить")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


def run_async(coro: Any) -> None:
    """Запустить async main() с корректной обработкой Ctrl-C."""
    import asyncio

    try:
        asyncio.run(coro)
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
