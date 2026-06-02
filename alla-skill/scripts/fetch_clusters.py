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
    build_alla_client,
    error_envelope,
    exit_with_error,
    handle_api_error,
    load_settings,
    open_testops_client,
    print_json,
    run_async,
)

logger = logging.getLogger("alla.skill.fetch_clusters")


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


async def _main_async(args: argparse.Namespace) -> None:
    try:
        # require_kb_dsn=False: DSN живёт на сервере; клиент шлёт результат
        # TestOps-триажа в alla-server, который и пишет в PostgreSQL.
        settings = load_settings(require_kb_dsn=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.clients.alla_api_client import AllaApiError
    from alla.services.triage_service import TriageService

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

    # Кластеризация — локально (raw). Merge rules, KB lookup, onboarding и
    # запись skill_run выполняет сервер (там же DSN).
    clustering_report = _cluster(report, settings)

    triage_json = report.model_dump(mode="json")
    clustering_json = (
        clustering_report.model_dump(mode="json")
        if clustering_report is not None
        else None
    )

    try:
        with build_alla_client(settings) as alla_client:
            response = alla_client.create_skill_run(triage_json, clustering_json)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json(response)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    run_async(_main_async(args))


if __name__ == "__main__":
    main(sys.argv[1:])
