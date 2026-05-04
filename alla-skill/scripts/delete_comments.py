#!/usr/bin/env python3
"""Удалить ранее запушенные [alla]-комментарии в указанном launch.

Тонкая обёртка над :class:`alla.services.comment_delete_service.CommentDeleteService`.
"""

from __future__ import annotations

import argparse
import sys

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    error_envelope,
    exit_with_error,
    load_settings,
    open_testops_client,
    print_json,
    run_async,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Удалить [alla]-комментарии тест-кейсов launch'а.",
    )
    parser.add_argument("--launch-id", required=True, type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.app_support import collect_test_case_ids, filter_failed_results
    from alla.services.comment_delete_service import CommentDeleteService

    async with open_testops_client(settings) as client:
        try:
            results = await client.get_all_test_results_for_launch(args.launch_id)
        except Exception as exc:
            exit_with_error(
                error_envelope(
                    f"Не удалось получить тесты для launch_id={args.launch_id}: {exc}",
                ),
                EXIT_ERROR,
            )
            return

        failed = filter_failed_results(results)
        test_case_ids, skipped_no_tc = collect_test_case_ids(failed)

        service = CommentDeleteService(
            client,
            concurrency=settings.detail_concurrency,
        )
        try:
            result = await service.delete_alla_comments(
                test_case_ids,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            exit_with_error(
                error_envelope(f"Ошибка удаления: {exc}"),
                EXIT_ERROR,
            )
            return

    print_json(
        {
            "ok": True,
            "launch_id": args.launch_id,
            "dry_run": args.dry_run,
            "test_cases_scanned": result.total_test_cases,
            "comments_found": result.comments_found,
            "comments_deleted": result.comments_deleted,
            "comments_failed": result.comments_failed,
            "skipped_test_cases": result.skipped_test_cases,
            "skipped_results_without_test_case_id": skipped_no_tc,
        }
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    run_async(_main_async(args))


if __name__ == "__main__":
    main(sys.argv[1:])
