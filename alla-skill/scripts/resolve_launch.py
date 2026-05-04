#!/usr/bin/env python3
"""Резолв launch_id по имени запуска в Allure TestOps.

Тонкая обёртка над ``AllureTestOpsClient.find_launch_by_name``.
"""

from __future__ import annotations

import argparse
import sys

from _common import (
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    error_envelope,
    exit_with_error,
    load_settings,
    open_testops_client,
    print_json,
    run_async,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Найти launch_id в Allure TestOps по имени.",
    )
    parser.add_argument("--name", required=True, help="Имя launch'а")
    parser.add_argument(
        "--project-id",
        type=int,
        default=None,
        help="ID проекта (по умолчанию — из ALLURE_PROJECT_ID)",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    project_id = args.project_id if args.project_id is not None else settings.project_id

    async with open_testops_client(settings) as client:
        try:
            launch_id = await client.find_launch_by_name(args.name, project_id)
        except Exception as exc:
            exit_with_error(
                error_envelope(
                    f"Не удалось найти launch '{args.name}': {exc}",
                    name=args.name,
                    project_id=project_id,
                ),
                EXIT_NOT_FOUND,
            )
            return

    print_json(
        {
            "ok": True,
            "launch_id": int(launch_id),
            "launch_name": args.name,
            "project_id": project_id,
        }
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    run_async(_main_async(args))


if __name__ == "__main__":
    main(sys.argv[1:])
