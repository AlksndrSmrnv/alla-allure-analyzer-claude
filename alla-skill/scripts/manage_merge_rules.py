#!/usr/bin/env python3
"""Manage cluster merge rules through alla-server REST API."""

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
        description="Управление merge rules через REST API alla-server.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Показать правила проекта")
    p_list.add_argument("--project-id", required=True, type=int)

    p_create = sub.add_parser("create", help="Создать или обновить правила")
    p_create.add_argument(
        "--json",
        default="-",
        help="Путь к JSON payload или '-' для stdin (default '-').",
    )

    p_delete = sub.add_parser("delete", help="Удалить правило")
    p_delete.add_argument("--rule-id", required=True, type=int)

    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_list(client: Any, project_id: int) -> None:
    response = client.list_merge_rules(project_id)
    print_json(
        {
            "ok": True,
            "count": len(response.rules),
            "rules": [rule.model_dump(mode="json") for rule in response.rules],
        }
    )


def _cmd_create(client: Any, payload: dict[str, Any]) -> None:
    from alla.clients.alla_api_client import AllaApiError
    from alla.knowledge.merge_rules_models import MergeRulesRequest

    try:
        request = MergeRulesRequest.model_validate(payload)
    except Exception as exc:
        exit_with_error(error_envelope(f"Невалидный payload: {exc}"), EXIT_VALIDATION)
        return

    try:
        response = client.create_merge_rules(request)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json({"ok": True, **response.model_dump(mode="json")})


def _cmd_delete(client: Any, rule_id: int) -> None:
    from alla.clients.alla_api_client import AllaApiError

    try:
        response = client.delete_merge_rule(rule_id)
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
        with build_alla_client(settings) as client:
            if args.cmd == "list":
                _cmd_list(client, args.project_id)
                return
            if args.cmd == "create":
                try:
                    payload = _read_payload(args.json)
                except Exception as exc:
                    exit_with_error(
                        error_envelope(f"Не удалось прочитать payload: {exc}"),
                        EXIT_VALIDATION,
                    )
                    return
                _cmd_create(client, payload)
                return
            if args.cmd == "delete":
                _cmd_delete(client, args.rule_id)
                return
    except Exception as exc:
        from alla.clients.alla_api_client import AllaApiError

        if isinstance(exc, AllaApiError):
            handle_api_error(exc)
        raise

    exit_with_error(error_envelope("Неизвестная команда"), EXIT_VALIDATION)


if __name__ == "__main__":
    main(sys.argv[1:])
