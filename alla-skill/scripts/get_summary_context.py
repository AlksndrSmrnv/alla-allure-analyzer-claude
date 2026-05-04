#!/usr/bin/env python3
"""Промпт + контекст для итогового launch summary.

Использует :func:`alla.services.prompt_builder_service.build_launch_summary_prompt`,
тот же, что server-side. На вход — ``run_id`` и (опционально) уже
полученные cluster-анализы для подмешивания в промпт; если их нет —
подставляются сырые данные кластеров.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
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


def _build_intermediate_llm_result(
    clustering_report: Any,
    cluster_analyses_payload: dict[str, Any] | None,
) -> Any | None:
    if not cluster_analyses_payload:
        return None
    from alla.models.llm import (
        LLMAnalysisResult,
        LLMClusterAnalysis,
        TokenUsage,
    )

    analyses: dict[str, LLMClusterAnalysis] = {}
    for cluster_id, item in cluster_analyses_payload.items():
        if not isinstance(item, dict):
            continue
        text = item.get("analysis_text") or ""
        analyses[cluster_id] = LLMClusterAnalysis(
            cluster_id=cluster_id,
            analysis_text=text,
        )
    return LLMAnalysisResult(
        total_clusters=len(clustering_report.clusters) if clustering_report else 0,
        analyzed_count=sum(1 for v in analyses.values() if v.analysis_text),
        failed_count=0,
        skipped_count=0,
        cluster_analyses=analyses,
        token_usage=TokenUsage(),
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.services.agent_analysis_adapter import agent_to_llm_result
    from alla.services.prompt_builder_service import build_launch_summary_prompt
    from alla.services.skill_state_service import SkillStateError, load_run

    try:
        skill_run = load_run(dsn=get_pg_dsn(settings), run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(error_envelope(str(exc), run_id=args.run_id), EXIT_NOT_FOUND)
        return

    if skill_run.clustering_report is None:
        exit_with_error(
            error_envelope(
                "В этом run нет ClusteringReport — нечего суммировать.",
                run_id=args.run_id,
            ),
            EXIT_NOT_FOUND,
        )
        return

    intermediate_result: Any | None = None
    if args.analyses_input == "-":
        try:
            payload = parse_stdin_json()
        except Exception as exc:
            exit_with_error(
                error_envelope(f"Не удалось прочитать stdin JSON: {exc}"),
                EXIT_NOT_FOUND,
            )
            return
        intermediate_result = _build_intermediate_llm_result(
            skill_run.clustering_report,
            payload.get("clusters") if isinstance(payload, dict) else None,
        )
    elif args.analyses_input:
        import json as _json
        try:
            with open(args.analyses_input, encoding="utf-8") as fh:
                payload = _json.load(fh)
        except Exception as exc:
            exit_with_error(
                error_envelope(f"Не удалось прочитать {args.analyses_input}: {exc}"),
                EXIT_NOT_FOUND,
            )
            return
        intermediate_result = _build_intermediate_llm_result(
            skill_run.clustering_report,
            payload.get("clusters") if isinstance(payload, dict) else None,
        )
    elif skill_run.agent_analysis is not None:
        # Уже сохранённый анализ — переиспользуем тот же адаптер.
        intermediate_result = agent_to_llm_result(
            skill_run.agent_analysis,
            skill_run.clustering_report,
        )

    prompt = build_launch_summary_prompt(
        skill_run.clustering_report,
        skill_run.triage_report,
        intermediate_result,
    )

    top_clusters: list[dict[str, Any]] = []
    for cluster in sorted(
        skill_run.clustering_report.clusters,
        key=lambda c: -c.member_count,
    )[:10]:
        top_clusters.append(
            {
                "cluster_id": cluster.cluster_id,
                "label": cluster.label,
                "size": cluster.member_count,
                "step_path": cluster.example_step_path,
                "kb_match_count": len(
                    skill_run.kb_results.get(cluster.cluster_id) or []
                ),
            }
        )

    response = {
        "ok": True,
        "run_id": args.run_id,
        "system_prompt": prompt.system_prompt,
        "user_prompt": prompt.user_prompt,
        "context": {
            "counters": {
                "total_results": skill_run.triage_report.total_results,
                "passed": skill_run.triage_report.passed_count,
                "failed": skill_run.triage_report.failed_count,
                "broken": skill_run.triage_report.broken_count,
                "skipped": skill_run.triage_report.skipped_count,
                "muted_failures": skill_run.triage_report.muted_failure_count,
                "active_failures": skill_run.triage_report.active_failure_count,
            },
            "cluster_count": skill_run.clustering_report.cluster_count,
            "top_clusters": top_clusters,
            "analyses_used": prompt.analyses_used,
        },
    }
    print_json(response)


if __name__ == "__main__":
    main(sys.argv[1:])
