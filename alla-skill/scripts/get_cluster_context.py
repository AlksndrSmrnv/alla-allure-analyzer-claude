#!/usr/bin/env python3
"""Контекст одного кластера + готовый промпт для агентского анализа.

Тонкая обёртка над :mod:`alla.services.skill_state_service` (читает
``alla.skill_run``) и :mod:`alla.services.prompt_builder_service` (строит
тот же промпт, что использует server-side GigaChat-путь).

Subagent читает stdout этого скрипта и применяет ``system_prompt`` +
``user_prompt`` для анализа одного кластера.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
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


def _find_cluster(clustering_report: Any, cluster_id: str) -> Any:
    for cluster in clustering_report.clusters:
        if cluster.cluster_id == cluster_id:
            return cluster
    return None


def _representative_payload(cluster: Any, test_by_id: dict[int, Any]) -> dict[str, Any] | None:
    rep_id = cluster.representative_test_id
    if rep_id is None:
        return None
    rep = test_by_id.get(rep_id)
    if rep is None:
        return None
    return {
        "test_result_id": rep.test_result_id,
        "test_case_id": rep.test_case_id,
        "name": rep.name,
        "full_name": rep.full_name,
        "link": rep.link,
        "status": rep.status.value,
        "status_message": rep.status_message,
        "status_trace": rep.status_trace,
        "log_snippet": rep.log_snippet,
        "failed_step_path": rep.failed_step_path,
    }


def _members_payload(cluster: Any, test_by_id: dict[int, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for tid in cluster.member_test_ids:
        member = test_by_id.get(tid)
        if member is None:
            continue
        payload.append(
            {
                "test_result_id": member.test_result_id,
                "test_case_id": member.test_case_id,
                "name": member.name,
                "link": member.link,
                "status": member.status.value,
            }
        )
    return payload


def _kb_match_payload(match: Any) -> dict[str, Any]:
    entry = match.entry
    return {
        "entry_id": entry.entry_id,
        "id": entry.id,
        "title": entry.title,
        "category": entry.category.value,
        "score": round(match.score, 3),
        "tier": _tier_label(match),
        "matched_on": list(match.matched_on or []),
        "match_origin": match.match_origin,
        "feedback_vote": match.feedback_vote,
        "description": entry.description,
        "step_path": entry.step_path,
        "error_example_preview": (entry.error_example or "")[:600],
        "resolution_steps": list(entry.resolution_steps),
    }


def _tier_label(match: Any) -> str:
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


def _select_log_and_trace(cluster: Any, test_by_id: dict[int, Any]) -> tuple[str | None, str | None]:
    log_snippet: str | None = None
    full_trace: str | None = None
    if cluster.representative_test_id is not None:
        rep = test_by_id.get(cluster.representative_test_id)
        if rep:
            if rep.log_snippet and rep.log_snippet.strip():
                log_snippet = rep.log_snippet
            full_trace = rep.status_trace
    if not log_snippet:
        for tid in cluster.member_test_ids:
            member = test_by_id.get(tid)
            if member and member.log_snippet and member.log_snippet.strip():
                log_snippet = member.log_snippet
                break
    return log_snippet, full_trace


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.services.prompt_builder_service import build_cluster_analysis_prompt
    from alla.services.skill_state_service import SkillStateError, load_run

    try:
        skill_run = load_run(dsn=get_pg_dsn(settings), run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(
            error_envelope(str(exc), run_id=args.run_id),
            EXIT_NOT_FOUND,
        )
        return

    if skill_run.clustering_report is None:
        exit_with_error(
            error_envelope(
                "В этом run нет ClusteringReport (нет падений?)",
                run_id=args.run_id,
            ),
            EXIT_ERROR,
        )
        return

    cluster = _find_cluster(skill_run.clustering_report, args.cluster_id)
    if cluster is None:
        exit_with_error(
            error_envelope(
                f"cluster_id={args.cluster_id!r} не найден",
                run_id=args.run_id,
            ),
            EXIT_NOT_FOUND,
        )
        return

    test_by_id = {t.test_result_id: t for t in skill_run.triage_report.failed_tests}
    log_snippet, full_trace = _select_log_and_trace(cluster, test_by_id)
    kb_matches = skill_run.kb_results.get(cluster.cluster_id) or []
    provenance = skill_run.kb_provenance.get(cluster.cluster_id)

    prompt = build_cluster_analysis_prompt(
        cluster,
        kb_matches=kb_matches,
        log_snippet=log_snippet,
        full_trace=full_trace,
        kb_query_provenance=provenance,
        message_max_chars=args.max_message_chars,
        trace_max_chars=args.max_trace_chars,
        log_max_chars=args.max_log_chars,
    )

    response: dict[str, Any] = {
        "ok": True,
        "run_id": args.run_id,
        "cluster_id": cluster.cluster_id,
        "system_prompt": prompt.system_prompt,
        "user_prompt": prompt.user_prompt,
        "context": {
            "label": cluster.label,
            "size": cluster.member_count,
            "step_path": cluster.example_step_path,
            "signature": cluster.signature.model_dump(mode="json"),
            "representative": _representative_payload(cluster, test_by_id),
            "members": _members_payload(cluster, test_by_id),
            "kb_matches": [_kb_match_payload(m) for m in kb_matches],
            "kb_query_provenance": (
                {
                    "message_chars": provenance[0],
                    "trace_chars": provenance[1],
                    "log_chars": provenance[2],
                }
                if provenance
                else None
            ),
            "feedback_context": (
                skill_run.feedback_contexts[cluster.cluster_id].model_dump(mode="json")
                if cluster.cluster_id in skill_run.feedback_contexts
                else None
            ),
        },
    }
    print_json(response)


if __name__ == "__main__":
    main(sys.argv[1:])
