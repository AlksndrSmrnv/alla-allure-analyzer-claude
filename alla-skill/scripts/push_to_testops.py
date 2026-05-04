#!/usr/bin/env python3
"""ШАГ 4 (опц.): постинг агентского анализа в Allure TestOps.

Тонкая обёртка над :func:`alla.services.comment_push_service.push_comments`
+ :func:`alla.app_support.attach_report_link`.

**Push выключен по умолчанию** (`ALLURE_PUSH_TO_TESTOPS=false`). Чтобы
выполнить реальный push, требуется `--confirm`. Без `--confirm` и без
`--dry-run` скрипт прерывается с envelope `push_disabled`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_VALIDATION,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
    load_settings,
    open_testops_client,
    print_json,
    run_async,
)

logger = logging.getLogger("alla.skill.push_to_testops")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Запушить агентский анализ в TestOps как [alla]-комментарии.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--attach-report-url",
        default=None,
        help="URL HTML-отчёта для прикрепления к launch'у через PATCH /api/launch.",
    )
    parser.add_argument(
        "--report-url-from-db",
        action="store_true",
        help="Использовать report_url, сохранённый ранее generate_report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Подготовить комментарии и распечатать, но НЕ постить.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Явное разрешение на реальный push (обязательно при ALLURE_PUSH_TO_TESTOPS=false).",
    )
    return parser


def _format_dry_run_preview(comments: dict[int, str]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for tc_id, text in list(comments.items())[:10]:
        head = text.splitlines()[:6]
        preview.append({"test_case_id": tc_id, "comment_preview": "\n".join(head)})
    return preview


def _build_dry_run_comments(skill_run: Any, llm_result: Any) -> dict[int, str]:
    from alla.services.comment_push_service import format_comment

    test_case_ids: dict[int, int | None] = {
        t.test_result_id: t.test_case_id for t in skill_run.triage_report.failed_tests
    }
    comments: dict[int, str] = {}
    if skill_run.clustering_report is None:
        return comments
    for cluster in skill_run.clustering_report.clusters:
        analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
        if not analysis or not analysis.analysis_text:
            continue
        text = format_comment(analysis.analysis_text, step_path=cluster.example_step_path)
        for test_id in cluster.member_test_ids:
            tc_id = test_case_ids.get(test_id)
            if tc_id is None or tc_id in comments:
                continue
            comments[tc_id] = text
    return comments


async def _main_async(args: argparse.Namespace) -> None:
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    # Push в TestOps по контракту скилла требует явного подтверждения от
    # пользователя. На общую настройку ``ALLURE_PUSH_TO_TESTOPS`` не
    # полагаемся — её дефолт в shared Settings ``True`` и тихо разрешил
    # бы push при пустом .env.
    if not args.dry_run and not args.confirm:
        exit_with_error(
            error_envelope(
                "push_disabled: для реального постинга комментариев нужен "
                "флаг --confirm (или --dry-run для предпросмотра). "
                "Скрипт никогда не пушит без явного подтверждения.",
                run_id=args.run_id,
            ),
            EXIT_VALIDATION,
        )
        return

    from alla.app_support import attach_report_link
    from alla.services.agent_analysis_adapter import agent_to_llm_result
    from alla.services.comment_push_service import push_comments
    from alla.services.skill_state_service import (
        SkillStateError,
        load_run,
        record_error,
        save_push_result,
    )

    dsn = get_pg_dsn(settings)
    try:
        skill_run = load_run(dsn=dsn, run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(error_envelope(str(exc), run_id=args.run_id), EXIT_NOT_FOUND)
        return

    if skill_run.agent_analysis is None or skill_run.clustering_report is None:
        exit_with_error(
            error_envelope(
                "Нет агентского анализа в skill_run — сначала вызови submit_analysis.",
                run_id=args.run_id,
            ),
            EXIT_NOT_FOUND,
        )
        return

    llm_result = agent_to_llm_result(
        skill_run.agent_analysis,
        skill_run.clustering_report,
    )

    if args.dry_run:
        comments = _build_dry_run_comments(skill_run, llm_result)
        print_json(
            {
                "ok": True,
                "run_id": args.run_id,
                "dry_run": True,
                "comments_planned": len(comments),
                "preview": _format_dry_run_preview(comments),
            }
        )
        return

    report_url: str | None = None
    if args.attach_report_url:
        report_url = args.attach_report_url
    elif args.report_url_from_db:
        report_url = skill_run.report_url

    async with open_testops_client(settings) as client:
        try:
            push_result = await push_comments(
                skill_run.clustering_report,
                llm_result,
                skill_run.triage_report,
                updater=client,
                concurrency=settings.detail_concurrency,
            )
        except Exception as exc:
            try:
                record_error(
                    dsn=dsn, run_id=args.run_id,
                    error={"step": "push_comments", "message": str(exc)},
                )
            except Exception:
                pass
            exit_with_error(
                error_envelope(f"Push упал: {exc}", run_id=args.run_id),
                EXIT_ERROR,
            )
            return

        link_attached = False
        if report_url:
            try:
                await attach_report_link(
                    client,
                    launch_id=skill_run.launch_id,
                    settings=settings,
                    report_url=report_url,
                )
                link_attached = True
            except Exception as exc:
                logger.warning("Не удалось прикрепить ссылку на отчёт: %s", exc)

    push_payload = {
        "total_tests": push_result.total_tests,
        "comments_posted": push_result.updated_count,
        "comments_failed": push_result.failed_count,
        "comments_skipped": push_result.skipped_count,
        "report_link_attached": link_attached,
        "report_url": report_url,
    }
    try:
        save_push_result(dsn=dsn, run_id=args.run_id, push_result=push_payload)
    except SkillStateError as exc:
        logger.warning("Push выполнен, но не удалось обновить skill_run: %s", exc)

    print_json({"ok": True, "run_id": args.run_id, **push_payload})


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    run_async(_main_async(args))


if __name__ == "__main__":
    main(sys.argv[1:])
