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
import sys

from _common import (
    EXIT_CONFIG,
    error_envelope,
    exit_with_error,
    load_settings,
)

logger = logging.getLogger("alla.skill.serve")


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
        f"[serve.py] PostgreSQL DSN: {settings.kb_postgres_dsn[:40]}…\n",
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
