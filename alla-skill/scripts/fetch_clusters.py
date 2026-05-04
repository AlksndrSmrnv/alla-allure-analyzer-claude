#!/usr/bin/env python3
"""ШАГ 1 pipeline скилл-режима: получить кластеры падений запуска.

Тонкая обёртка над публичными сервисами:

* :class:`alla.services.triage_service.TriageService`
* :class:`alla.services.log_extraction_service.LogExtractionService`
* :class:`alla.services.clustering_service.ClusteringService`
* :func:`alla.orchestrator.apply_merge_rules_phase`
* :func:`alla.services.kb_lookup_service.lookup_kb_for_clusters`
* :func:`alla.orchestrator.build_onboarding_state`

Шаги pipeline идут в том же порядке и под теми же gate'ами, что
``alla.orchestrator.analyze_launch`` — это обеспечивает, что серверная
alla и skill-режим строят одинаковые кластеры и контексты для одного
launch_id.

Складывает результат в ``alla.skill_run`` и печатает компактный JSON
с ``run_id`` + сводкой по кластерам.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
    load_settings,
    open_testops_client,
    print_json,
    run_async,
)

logger = logging.getLogger("alla.skill.fetch_clusters")

_MESSAGE_PREVIEW_CHARS = 240


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline: triage → logs → clustering → merge_rules → KB.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--launch-id", type=int)
    target.add_argument("--launch-name", type=str)
    parser.add_argument(
        "--project-id",
        type=int,
        default=None,
        help="Нужен с --launch-name; иначе берётся из настроек",
    )
    return parser


async def _resolve_launch_id(client: Any, args: argparse.Namespace, settings: Any) -> int:
    if args.launch_id is not None:
        return int(args.launch_id)
    project_id = (
        args.project_id if args.project_id is not None else settings.project_id
    )
    return await client.find_launch_by_name(args.launch_name, project_id)


async def _enrich_with_logs(report: Any, client: Any, settings: Any) -> None:
    from alla.clients.base import AttachmentProvider
    from alla.services.log_extraction_service import (
        LogExtractionConfig,
        LogExtractionService,
    )

    if not report.failed_tests:
        return
    if not isinstance(client, AttachmentProvider):
        logger.debug("Клиент не реализует AttachmentProvider — лог-обогащение пропущено.")
        return
    log_service = LogExtractionService(
        client,
        LogExtractionConfig(concurrency=settings.logs_concurrency),
    )
    try:
        await log_service.enrich_with_logs(report.failed_tests)
    except Exception as exc:
        logger.warning("Log enrichment: ошибка: %s", exc)


def _cluster(report: Any, settings: Any) -> Any:
    if not report.failed_tests:
        return None
    from alla.services.clustering_service import ClusteringConfig, ClusteringService

    service = ClusteringService(
        ClusteringConfig(
            similarity_threshold=settings.clustering_threshold,
            log_similarity_weight=settings.logs_clustering_weight,
        )
    )
    return service.cluster_failures(report.launch_id, report.failed_tests)


def _build_response(run_id: int, report: Any, clustering_report: Any, kb_results: dict[str, list[Any]]) -> dict[str, Any]:
    counters = {
        "total_results": report.total_results,
        "passed": report.passed_count,
        "failed": report.failed_count,
        "broken": report.broken_count,
        "skipped": report.skipped_count,
        "unknown": report.unknown_count,
        "muted_failures": report.muted_failure_count,
        "active_failures": report.active_failure_count,
    }
    clusters_view: list[dict[str, Any]] = []
    cluster_count = 0
    unclustered_count = 0
    if clustering_report is not None:
        cluster_count = clustering_report.cluster_count
        unclustered_count = clustering_report.unclustered_count
        for cluster in clustering_report.clusters:
            matches = kb_results.get(cluster.cluster_id) or []
            top = matches[0] if matches else None
            top_view = None
            if top is not None:
                top_view = {
                    "title": top.entry.title,
                    "score": round(top.score, 3),
                    "tier": _short_tier(top),
                    "category": top.entry.category.value,
                    "entry_id": top.entry.entry_id,
                }
            preview = (cluster.example_message or "").strip()
            if len(preview) > _MESSAGE_PREVIEW_CHARS:
                preview = preview[:_MESSAGE_PREVIEW_CHARS] + "…"
            clusters_view.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "label": cluster.label,
                    "size": cluster.member_count,
                    "representative_test_id": cluster.representative_test_id,
                    "example_step_path": cluster.example_step_path,
                    "message_preview": preview,
                    "kb_match_count": len(matches),
                    "top_kb_match": top_view,
                }
            )
    return {
        "ok": True,
        "run_id": run_id,
        "launch": {
            "id": report.launch_id,
            "name": report.launch_name,
            "project_id": report.project_id,
        },
        "counters": counters,
        "cluster_count": cluster_count,
        "unclustered_count": unclustered_count,
        "clusters": clusters_view,
    }


def _short_tier(match: Any) -> str:
    if match.match_origin == "feedback_exact":
        return "feedback_exact"
    for reason in match.matched_on or []:
        if "Tier 1" in reason or "exact substring" in reason.lower():
            return "Tier 1"
        if "Tier 2" in reason:
            return "Tier 2"
        if "Tier 3" in reason or "TF-IDF" in reason:
            return "Tier 3"
    return "unknown"


async def _main_async(args: argparse.Namespace) -> None:
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.orchestrator import apply_merge_rules_phase, build_onboarding_state
    from alla.services.kb_lookup_service import KBStageResult, lookup_kb_for_clusters
    from alla.services.skill_state_service import create_run, record_error
    from alla.services.triage_service import TriageService

    dsn = get_pg_dsn(settings)

    async with open_testops_client(settings) as client:
        try:
            launch_id = await _resolve_launch_id(client, args, settings)
        except Exception as exc:
            exit_with_error(
                error_envelope(f"Не удалось определить launch_id: {exc}"),
                EXIT_ERROR,
            )
            return

        try:
            report = await TriageService(client, settings).analyze_launch(launch_id)
        except Exception as exc:
            exit_with_error(
                error_envelope(
                    f"Triage упал для launch_id={launch_id}: {exc}",
                    launch_id=launch_id,
                ),
                EXIT_ERROR,
            )
            return

        await _enrich_with_logs(report, client, settings)

    clustering_report = _cluster(report, settings)
    clustering_report = apply_merge_rules_phase(report, clustering_report, settings)

    try:
        kb_stage = lookup_kb_for_clusters(report, clustering_report, settings)
    except Exception as exc:
        logger.warning("KB lookup: ошибка: %s", exc)
        kb_stage = KBStageResult()

    onboarding = build_onboarding_state(
        settings,
        report.project_id,
        clustering_report,
        kb_entries=kb_stage.kb_entries,
    )

    try:
        run_id = create_run(
            dsn=dsn,
            triage_report=report,
            clustering_report=clustering_report,
            kb_stage=kb_stage,
            onboarding=onboarding,
        )
    except Exception as exc:
        exit_with_error(
            error_envelope(f"Не удалось записать alla.skill_run: {exc}"),
            EXIT_ERROR,
        )
        return

    try:
        response = _build_response(run_id, report, clustering_report, kb_stage.kb_results)
        print_json(response)
    except Exception as exc:
        try:
            record_error(dsn=dsn, run_id=run_id, error={"step": "build_response", "message": str(exc)})
        except Exception:
            pass
        exit_with_error(
            error_envelope(f"Ошибка формирования ответа: {exc}", run_id=run_id),
            EXIT_ERROR,
        )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    run_async(_main_async(args))


if __name__ == "__main__":
    main(sys.argv[1:])
