#!/usr/bin/env python3
"""CRUD KB-записей через REST API alla-server."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
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
        description="CRUD KB-записей через REST API alla-server.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Показать все записи")
    p_list.add_argument("--project-id", type=int, default=None)

    p_create = sub.add_parser("create", help="Создать новую запись")
    p_create.add_argument(
        "--json", default="-",
        help="Путь к JSON-файлу или '-' для stdin (default '-').",
    )

    p_update = sub.add_parser("update", help="Обновить запись")
    p_update.add_argument("--entry-id", required=True, type=int)
    p_update.add_argument(
        "--json", default="-",
        help="Путь к JSON-патчу или '-' для stdin (default '-').",
    )

    p_delete = sub.add_parser("delete", help="Удалить запись")
    p_delete.add_argument("--entry-id", required=True, type=int)
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="Удалить запись даже если на неё есть feedback-голоса.",
    )

    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_list(client: Any, project_id: int | None) -> None:
    entries = client.list_kb_entries(project_id)
    payload = [
        {
            "entry_id": entry.entry_id,
            "id": entry.id,
            "title": entry.title,
            "category": entry.category.value,
            "project_id": entry.project_id,
            "step_path": entry.step_path,
            "error_example_chars": len(entry.error_example or ""),
            "resolution_steps_count": len(entry.resolution_steps),
        }
        for entry in entries
    ]
    print_json({"ok": True, "count": len(payload), "entries": payload})


def _cmd_create(client: Any, payload: dict[str, Any]) -> None:
    from alla.clients.alla_api_client import AllaApiError
    from alla.knowledge.feedback_models import CreateKBEntryRequest

    try:
        request = CreateKBEntryRequest.model_validate(payload)
    except Exception as exc:
        exit_with_error(error_envelope(f"Невалидный payload: {exc}"), EXIT_VALIDATION)
        return

    try:
        response, created = client.create_kb_entry(request)
    except AllaApiError as exc:
        handle_api_error(exc)

    out = response.model_dump(mode="json")
    out["created"] = created
    print_json({"ok": True, **out})


def _cmd_update(client: Any, entry_id: int, payload: dict[str, Any]) -> None:
    from alla.clients.alla_api_client import AllaApiError

    if not isinstance(payload, dict):
        exit_with_error(error_envelope("Payload должен быть JSON-объектом"), EXIT_VALIDATION)
        return

    try:
        response = client.update_kb_entry(entry_id, payload)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json({"ok": True, **response})


def _cmd_delete(client: Any, entry_id: int, *, force: bool) -> None:
    from alla.clients.alla_api_client import AllaApiConflictError, AllaApiError

    try:
        response = client.delete_kb_entry(entry_id, force=force)
    except AllaApiConflictError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        feedback_count = payload.get("feedback_count")
        exit_with_error(
            error_envelope(
                f"Удаление отклонено: {exc.detail}. "
                "Повтори команду с --force, если нужно удалить запись и связанные голоса.",
                status_code=exc.status_code,
                feedback_count=feedback_count,
            ),
            EXIT_ERROR,
        )
        return
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
            if args.cmd == "update":
                try:
                    payload = _read_payload(args.json)
                except Exception as exc:
                    exit_with_error(
                        error_envelope(f"Не удалось прочитать payload: {exc}"),
                        EXIT_VALIDATION,
                    )
                    return
                _cmd_update(client, args.entry_id, payload)
                return
            if args.cmd == "delete":
                _cmd_delete(client, args.entry_id, force=args.force)
                return
    except Exception as exc:
        from alla.clients.alla_api_client import AllaApiError

        if isinstance(exc, AllaApiError):
            handle_api_error(exc)
        raise

    exit_with_error(error_envelope("Неизвестная команда"), EXIT_VALIDATION)


if __name__ == "__main__":
    main(sys.argv[1:])
