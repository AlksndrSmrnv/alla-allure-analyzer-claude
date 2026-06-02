#!/usr/bin/env python3
"""Принять агентский анализ через stdin и записать через ``alla-server``.

Тонкая обёртка над REST-эндпоинтом
``POST /api/v1/skill/runs/{run_id}/analysis``. Валидацию схемы и запись в
``alla.skill_run`` выполняет сервер (DSN живёт только на сервере).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_VALIDATION,
    build_alla_client,
    error_envelope,
    exit_with_error,
    handle_api_error,
    load_settings,
    parse_stdin_json,
    print_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Записать агентский анализ кластеров в alla.skill_run.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--input",
        default="-",
        help="Путь к JSON-файлу или '-' для stdin (default '-').",
    )
    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(require_kb_dsn=False, validate_testops=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    try:
        payload = _read_payload(args.input)
    except Exception as exc:
        exit_with_error(
            error_envelope(f"Не удалось прочитать payload: {exc}"),
            EXIT_VALIDATION,
        )
        return

    if not isinstance(payload, dict):
        exit_with_error(
            error_envelope("payload должен быть JSON-объектом"),
            EXIT_VALIDATION,
        )
        return

    from alla.clients.alla_api_client import AllaApiError

    try:
        with build_alla_client(settings) as client:
            response = client.submit_skill_analysis(args.run_id, payload)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json(response)


if __name__ == "__main__":
    main(sys.argv[1:])
