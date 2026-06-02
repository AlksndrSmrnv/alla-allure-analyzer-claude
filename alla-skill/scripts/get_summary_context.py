#!/usr/bin/env python3
"""Промпт + контекст для итогового launch summary.

Тонкая обёртка над REST-эндпоинтом ``alla-server``
``POST /api/v1/skill/runs/{run_id}/summary-context``. Сервер читает
``alla.skill_run`` и строит тот же промпт, что server-side путь.

На вход — ``run_id`` и (опционально) уже полученные cluster-анализы для
подмешивания в промпт; если их нет — на сервере подставляются сохранённые
в ``alla.skill_run``.
"""

from __future__ import annotations

import argparse
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
        description="Промпт + контекст для launch summary.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--analyses-input",
        default=None,
        help=(
            "Опц.: путь к файлу с уже собранными per-cluster анализами "
            "(для использования до submit_analysis). "
            "'-' для stdin. Если не указан, берутся сохранённые в alla.skill_run."
        ),
    )
    return parser


def _read_intermediate_clusters(source: str | None) -> dict[str, Any] | None:
    """Прочитать per-cluster анализы из stdin/файла и вернуть `clusters`."""
    if not source:
        return None
    if source == "-":
        payload = parse_stdin_json()
    else:
        import json as _json

        with open(source, encoding="utf-8") as fh:
            payload = _json.load(fh)
    if isinstance(payload, dict):
        clusters = payload.get("clusters")
        if isinstance(clusters, dict):
            return clusters
    return None


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(require_kb_dsn=False, validate_testops=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    try:
        intermediate = _read_intermediate_clusters(args.analyses_input)
    except Exception as exc:
        exit_with_error(
            error_envelope(f"Не удалось прочитать analyses-input: {exc}"),
            EXIT_VALIDATION,
        )
        return

    from alla.clients.alla_api_client import AllaApiError

    try:
        with build_alla_client(settings) as client:
            response = client.get_summary_context(args.run_id, intermediate)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json(response)


if __name__ == "__main__":
    main(sys.argv[1:])
