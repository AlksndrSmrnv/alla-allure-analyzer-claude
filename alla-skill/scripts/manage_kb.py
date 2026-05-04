#!/usr/bin/env python3
"""CRUD KB-записей в ``alla.kb_entry``.

Subcommands:

* ``list`` — все записи (опционально по project_id).
* ``create`` — INSERT новой записи (JSON через ``--json -`` или файл).
* ``update`` — UPDATE существующей записи по ``--entry-id``.
* ``delete`` — DELETE записи по ``--entry-id``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_VALIDATION,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
    load_settings,
    parse_stdin_json,
    print_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CRUD KB-записей для skill-режима.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Показать все записи")
    p_list.add_argument("--project-id", type=int, default=None)

    p_create = sub.add_parser("create", help="INSERT новой записи")
    p_create.add_argument(
        "--json", default="-",
        help="Путь к JSON-файлу или '-' для stdin (default '-').",
    )

    p_update = sub.add_parser("update", help="UPDATE записи")
    p_update.add_argument("--entry-id", required=True, type=int)
    p_update.add_argument(
        "--json", default="-",
        help="Путь к JSON-патчу или '-' для stdin (default '-').",
    )

    p_delete = sub.add_parser("delete", help="DELETE записи")
    p_delete.add_argument("--entry-id", required=True, type=int)

    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_list(dsn: str, project_id: int | None) -> None:
    if project_id is None:
        query = (
            "SELECT entry_id, id, title, category, project_id, "
            "step_path, length(error_example) AS example_chars, "
            "array_length(resolution_steps, 1) AS resolution_steps_count "
            "FROM alla.kb_entry ORDER BY project_id NULLS FIRST, id"
        )
        params: tuple[Any, ...] = ()
    else:
        query = (
            "SELECT entry_id, id, title, category, project_id, "
            "step_path, length(error_example) AS example_chars, "
            "array_length(resolution_steps, 1) AS resolution_steps_count "
            "FROM alla.kb_entry "
            "WHERE project_id IS NULL OR project_id = %s "
            "ORDER BY project_id NULLS FIRST, id"
        )
        params = (project_id,)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    payload = [
        {
            "entry_id": row[0],
            "id": row[1],
            "title": row[2],
            "category": row[3],
            "project_id": row[4],
            "step_path": row[5],
            "error_example_chars": row[6],
            "resolution_steps_count": row[7] or 0,
        }
        for row in rows
    ]
    print_json({"ok": True, "count": len(payload), "entries": payload})


def _cmd_create(dsn: str, payload: dict[str, Any]) -> None:
    from alla.knowledge.feedback_models import CreateKBEntryRequest
    from alla.knowledge.models import KBEntry, RootCauseCategory
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

    try:
        request = CreateKBEntryRequest.model_validate(payload)
    except Exception as exc:
        exit_with_error(error_envelope(f"Невалидный payload: {exc}"), EXIT_VALIDATION)
        return

    slug = request.id or _slugify(request.title or request.error_example or "kb-entry")
    title = request.title or _short_title(request.error_example) or slug

    entry = KBEntry(
        id=slug,
        title=title,
        description=request.description,
        error_example=request.error_example,
        step_path=request.step_path,
        category=request.category or RootCauseCategory.SERVICE,
        resolution_steps=list(request.resolution_steps),
    )
    store = PostgresFeedbackStore(dsn=dsn)
    try:
        entry_id = store.create_kb_entry(entry, request.project_id)
    except Exception as exc:
        exit_with_error(error_envelope(f"INSERT упал: {exc}"), EXIT_ERROR)
        return

    print_json(
        {
            "ok": True,
            "entry_id": entry_id,
            "id": slug,
            "title": title,
            "category": entry.category.value,
            "created": entry_id is not None,
        }
    )


def _cmd_update(dsn: str, entry_id: int, payload: dict[str, Any]) -> None:
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

    if not isinstance(payload, dict):
        exit_with_error(error_envelope("Payload должен быть JSON-объектом"), EXIT_VALIDATION)
        return

    store = PostgresFeedbackStore(dsn=dsn)
    try:
        updated = store.update_kb_entry(entry_id, payload)
    except Exception as exc:
        exit_with_error(error_envelope(f"UPDATE упал: {exc}"), EXIT_ERROR)
        return

    print_json({"ok": True, "entry_id": entry_id, "updated": updated})


def _cmd_delete(dsn: str, entry_id: int) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM alla.kb_entry WHERE entry_id = %s",
                (entry_id,),
            )
            deleted = cur.rowcount
            conn.commit()
    if deleted == 0:
        exit_with_error(
            error_envelope("Запись не найдена", entry_id=entry_id),
            EXIT_NOT_FOUND,
        )
        return
    print_json({"ok": True, "entry_id": entry_id, "deleted": True})


def _slugify(value: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:80] or "kb_entry"


def _short_title(value: str) -> str:
    head = value.strip().split("\n", 1)[0]
    return head[:80]


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return
    dsn = get_pg_dsn(settings)

    if args.cmd == "list":
        _cmd_list(dsn, args.project_id)
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
        _cmd_create(dsn, payload)
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
        _cmd_update(dsn, args.entry_id, payload)
        return
    if args.cmd == "delete":
        _cmd_delete(dsn, args.entry_id)
        return

    exit_with_error(error_envelope("Неизвестная команда"), EXIT_VALIDATION)


if __name__ == "__main__":
    main(sys.argv[1:])
