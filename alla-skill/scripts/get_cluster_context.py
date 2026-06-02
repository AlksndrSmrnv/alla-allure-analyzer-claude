#!/usr/bin/env python3
"""Контекст одного кластера + готовый промпт для агентского анализа.

Тонкая обёртка над REST-эндпоинтом ``alla-server``
``GET /api/v1/skill/runs/{run_id}/clusters/{cluster_id}/context``.
Сам сервер читает ``alla.skill_run`` и строит тот же промпт, что
использует server-side GigaChat-путь (DSN живёт только на сервере).

Subagent читает stdout этого скрипта и применяет ``system_prompt`` +
``user_prompt`` для анализа одного кластера.
"""

from __future__ import annotations

import argparse
import sys

from _common import (
    EXIT_CONFIG,
    build_alla_client,
    error_envelope,
    exit_with_error,
    handle_api_error,
    load_settings,
    print_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Контекст и промпт для анализа одного кластера.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument(
        "--max-log-chars", type=int, default=8000,
        help="Максимум символов лога в промпте",
    )
    parser.add_argument(
        "--max-message-chars", type=int, default=2000,
        help="Максимум символов сообщения об ошибке в промпте",
    )
    parser.add_argument(
        "--max-trace-chars", type=int, default=400,
        help="Максимум символов стек-трейса в промпте",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(require_kb_dsn=False, validate_testops=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.clients.alla_api_client import AllaApiError

    try:
        with build_alla_client(settings) as client:
            response = client.get_cluster_context(
                args.run_id,
                args.cluster_id,
                max_log_chars=args.max_log_chars,
                max_message_chars=args.max_message_chars,
                max_trace_chars=args.max_trace_chars,
            )
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json(response)


if __name__ == "__main__":
    main(sys.argv[1:])
