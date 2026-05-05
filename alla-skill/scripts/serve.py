#!/usr/bin/env python3
"""Локальный лаунчер `alla-server` для скилл-режима.

HTML-отчёт скилла использует REST-эндпоинты `alla-server`, чтобы кнопки
«Создать решение для кластера», like/dislike и merge rules писали в
PostgreSQL. Этот скрипт поднимает существующий FastAPI-app
(``alla.server.app``) на ``127.0.0.1`` рядом со скиллом — никаких
отдельных процессов на проде не нужно.

Использование::

    # Терминал 1
    python alla-skill/scripts/serve.py

    # alla-skill/.env
    ALLURE_FEEDBACK_SERVER_URL=http://127.0.0.1:8090
    ALLURE_SERVER_EXTERNAL_URL=http://127.0.0.1:8090

    # Терминал 2
    python alla-skill/scripts/generate_report.py --run-id <id>
    # → отчёт по http://127.0.0.1:8090/reports/<filename>

REST-эндпоинты пишут в ту же PostgreSQL, что и `fetch_clusters` /
`submit_analysis`. Никакого отдельного storage.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from urllib.parse import urlparse

from _common import (
    ENV_PATH,
    EXIT_CONFIG,
    error_envelope,
    exit_with_error,
    load_settings,
)

logger = logging.getLogger("alla.skill.serve")


def _mask_dsn(dsn: str) -> str:
    """Безопасно отрендерить DSN для логов: только host/db, без credentials.

    Стандартный PostgreSQL DSN — ``postgresql://user:password@host:5432/db``.
    Просто обрезать строку нельзя: префикс с паролем — самая чувствительная
    часть. Возвращаем ``host:port/db`` (без user/pass) или строку
    ``<unparsable>``, если URL не распарсился.
    """
    if not dsn:
        return "<empty>"
    try:
        parsed = urlparse(dsn)
    except Exception:
        return "<unparsable>"
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    db = parsed.path.lstrip("/") or "?"
    return f"{host}{port}/{db}"


def _propagate_env_to_server() -> None:
    """Загрузить ``alla-skill/.env`` в ``os.environ``.

    ``alla.server`` использует ``Settings()`` без явного ``_env_file``, поэтому
    в lifespan читает ``.env`` из CWD процесса. Если пользователь запускает
    ``serve.py`` из корня репо, сервер увидит другой ``.env`` (или вовсе
    никакой) — и БД/URL могут разъехаться с тем, что использует
    ``generate_report.py``. Перебрасываем переменные из скилл-``.env`` в
    ``os.environ`` ДО импорта ``alla.server`` и старта uvicorn, чтобы и
    скилл-скрипты, и сервер видели одинаковые значения.

    Не перезаписываем уже выставленные env vars: пользовательский экспорт
    в shell должен иметь приоритет, как и у pydantic-settings по умолчанию.
    """
    if not ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv  # python-dotenv тянется с pydantic-settings

        load_dotenv(dotenv_path=str(ENV_PATH), override=False)
    except ImportError:
        # Минимальный fallback на случай отсутствия python-dotenv:
        # читаем `KEY=VALUE` без экранирования, без секций.
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Локальный alla-server для интерактивного HTML-отчёта.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host для bind. По умолчанию loopback (127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Порт. По умолчанию 8090.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn log level (debug/info/warning/error).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    # Сначала проталкиваем skill `.env` в os.environ, чтобы lifespan
    # `alla.server` (который сам зовёт `Settings()` без `_env_file`)
    # увидел те же значения, что и `_common.load_settings`.
    _propagate_env_to_server()

    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    expected_url = f"http://{args.host}:{args.port}"
    print(
        f"\n[serve.py] Локальный alla-server поднимается на {expected_url}.\n"
        f"[serve.py] Чтобы HTML-отчёт получил интерактивные кнопки, в alla-skill/.env\n"
        f"[serve.py]   ALLURE_FEEDBACK_SERVER_URL={expected_url}\n"
        f"[serve.py]   ALLURE_SERVER_EXTERNAL_URL={expected_url}\n"
        f"[serve.py] и перезапусти generate_report.py.\n"
        f"[serve.py] PostgreSQL: {_mask_dsn(settings.kb_postgres_dsn)}\n",
        file=sys.stderr,
        flush=True,
    )

    import uvicorn  # local import чтобы --help не тянул FastAPI стек

    from alla.server import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
