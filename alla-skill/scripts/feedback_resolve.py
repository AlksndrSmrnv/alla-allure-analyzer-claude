#!/usr/bin/env python3
"""Resolve exact KB feedback votes through alla-server REST API."""

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
        description="Resolve feedback-голоса по exact issue signatures.",
    )
    parser.add_argument(
        "--json",
        default="-",
        help="Путь к JSON payload или '-' для stdin (default '-').",
    )
    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_resolve(client: Any, payload: dict[str, Any]) -> None:
    from alla.clients.alla_api_client import AllaApiError
    from alla.knowledge.feedback_models import FeedbackResolveRequest

    try:
        request = FeedbackResolveRequest.model_validate(payload)
    except Exception as exc:
        exit_with_error(error_envelope(f"Невалидный payload: {exc}"), EXIT_VALIDATION)
        return

    try:
        response = client.resolve_feedback(request)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json({"ok": True, **response.model_dump(mode="json")})


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(require_kb_dsn=False, validate_testops=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    try:
        payload = _read_payload(args.json)
    except Exception as exc:
        exit_with_error(
            error_envelope(f"Не удалось прочитать payload: {exc}"),
            EXIT_VALIDATION,
        )
        return

    with build_alla_client(settings) as client:
        _cmd_resolve(client, payload)


if __name__ == "__main__":
    main(sys.argv[1:])
