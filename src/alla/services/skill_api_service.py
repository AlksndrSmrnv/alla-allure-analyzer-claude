"""Чистые трансформации для skill REST API (`/api/v1/skill/...`).

Эти функции раньше жили в скрипт-обёртках ``alla-skill/scripts/`` и
обращались к PostgreSQL напрямую. Теперь скрипты ходят через
``alla-server``, а вся логика построения сводок/контекстов/промптов
переехала сюда — один источник истины для server-side.

Функции здесь не трогают БД сами: они принимают уже загруженный
:class:`alla.services.skill_state_service.SkillRun` (или доменные
объекты) и возвращают JSON-сериализуемые dict'ы.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alla.config import Settings
    from alla.models.clustering import ClusteringReport, FailureCluster
    from alla.models.testops import TriageReport
    from alla.knowledge.models import KBMatchResult
    from alla.orchestrator import AnalysisResult
    from alla.services.skill_state_service import SkillRun

_MESSAGE_PREVIEW_CHARS = 240


# ---------------------------------------------------------------------------
# Tier labels
# ---------------------------------------------------------------------------


def _short_tier(match: "KBMatchResult") -> str:
    """Краткий ярлык tier'а для сводки кластеров."""
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


# ---------------------------------------------------------------------------
# POST /skill/runs — сводка по запуску
# ---------------------------------------------------------------------------


def build_run_summary(
    run_id: int,
    report: "TriageReport",
    clustering_report: "ClusteringReport | None",
    kb_results: dict[str, list["KBMatchResult"]],
) -> dict[str, Any]:
    """Компактная сводка прогона + кластеров (ответ POST /skill/runs).

    Совпадает по форме с тем, что раньше печатал
    ``fetch_clusters._build_response``.
    """
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


# ---------------------------------------------------------------------------
# GET /skill/runs/{id}/clusters/{cid}/context — контекст + промпт кластера
# ---------------------------------------------------------------------------


def _tier_label(match: "KBMatchResult") -> str:
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


def _find_cluster(
    clustering_report: "ClusteringReport", cluster_id: str
) -> "FailureCluster | None":
    for cluster in clustering_report.clusters:
        if cluster.cluster_id == cluster_id:
            return cluster
    return None


def _representative_payload(
    cluster: "FailureCluster", test_by_id: dict[int, Any]
) -> dict[str, Any] | None:
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


def _members_payload(
    cluster: "FailureCluster", test_by_id: dict[int, Any]
) -> list[dict[str, Any]]:
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


def _kb_match_payload(match: "KBMatchResult") -> dict[str, Any]:
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


def _select_log_and_trace(
    cluster: "FailureCluster", test_by_id: dict[int, Any]
) -> tuple[str | None, str | None]:
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


def build_cluster_context(
    skill_run: "SkillRun",
    cluster_id: str,
    *,
    max_message_chars: int = 2000,
    max_trace_chars: int = 400,
    max_log_chars: int = 8000,
) -> dict[str, Any] | None:
    """Контекст + промпт для анализа одного кластера.

    Возвращает ``None``, если кластер не найден или нет ClusteringReport.
    Совпадает по форме с тем, что раньше печатал ``get_cluster_context``.
    """
    from alla.services.prompt_builder_service import build_cluster_analysis_prompt

    if skill_run.clustering_report is None:
        return None

    cluster = _find_cluster(skill_run.clustering_report, cluster_id)
    if cluster is None:
        return None

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
        message_max_chars=max_message_chars,
        trace_max_chars=max_trace_chars,
        log_max_chars=max_log_chars,
    )

    return {
        "ok": True,
        "run_id": skill_run.run_id,
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


# ---------------------------------------------------------------------------
# POST /skill/runs/{id}/summary-context — промпт launch summary
# ---------------------------------------------------------------------------


def _build_intermediate_llm_result(
    clustering_report: "ClusteringReport | None",
    cluster_analyses_payload: dict[str, Any] | None,
) -> Any | None:
    if not cluster_analyses_payload:
        return None
    from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, TokenUsage

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


def build_summary_context(
    skill_run: "SkillRun",
    intermediate_clusters: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Промпт + контекст для launch summary.

    ``intermediate_clusters`` — опциональные per-cluster анализы до submit
    (``{cluster_id: {"analysis_text": ...}}``). Если не заданы, берётся
    сохранённый агентский анализ из ``skill_run``.

    Возвращает ``None``, если в run нет ClusteringReport.
    """
    from alla.services.agent_analysis_adapter import agent_to_llm_result
    from alla.services.prompt_builder_service import build_launch_summary_prompt

    if skill_run.clustering_report is None:
        return None

    intermediate_result: Any | None = None
    if intermediate_clusters:
        intermediate_result = _build_intermediate_llm_result(
            skill_run.clustering_report,
            intermediate_clusters,
        )
    elif skill_run.agent_analysis is not None:
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

    return {
        "ok": True,
        "run_id": skill_run.run_id,
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


# ---------------------------------------------------------------------------
# GET /skill/runs/{id} — полная сериализация run
# ---------------------------------------------------------------------------


def serialize_run(skill_run: "SkillRun") -> dict[str, Any]:
    """Сериализовать ``SkillRun`` в JSON для локального восстановления.

    Используется ``push_to_testops`` и ``record_feedback``, которые
    восстанавливают доменные модели через ``*.model_validate``.
    """
    clustering = (
        skill_run.clustering_report.model_dump(mode="json")
        if skill_run.clustering_report is not None
        else None
    )
    kb_results = {
        cluster_id: [m.model_dump(mode="json") for m in matches]
        for cluster_id, matches in skill_run.kb_results.items()
    }
    kb_provenance = {
        cluster_id: list(values)
        for cluster_id, values in skill_run.kb_provenance.items()
    }
    feedback_contexts = {
        cluster_id: ctx.model_dump(mode="json")
        for cluster_id, ctx in skill_run.feedback_contexts.items()
    }
    return {
        "ok": True,
        "run_id": skill_run.run_id,
        "schema_version": skill_run.schema_version,
        "status": skill_run.status,
        "launch_id": skill_run.launch_id,
        "project_id": skill_run.project_id,
        "launch_name": skill_run.launch_name,
        "triage_report": skill_run.triage_report.model_dump(mode="json"),
        "clustering_report": clustering,
        "kb_results": kb_results,
        "kb_provenance": kb_provenance,
        "feedback_contexts": feedback_contexts,
        "onboarding": skill_run.onboarding.model_dump(mode="json"),
        "agent_analysis": skill_run.agent_analysis,
        "agent_summary_text": skill_run.agent_summary_text,
        "report_filename": skill_run.report_filename,
        "report_url": skill_run.report_url,
        "push_result": skill_run.push_result,
        "error": skill_run.error,
    }


# ---------------------------------------------------------------------------
# POST /skill/runs/{id}/report — построение AnalysisResult для HTML
# ---------------------------------------------------------------------------


def interactive_disabled_reasons(settings: "Settings") -> list[str]:
    """Причины, по которым в HTML нет KB-кнопок и like/dislike."""
    reasons: list[str] = []
    if not settings.kb_active:
        reasons.append("kb_inactive")
    elif not settings.feedback_server_url:
        reasons.append("feedback_server_url_empty")
    return reasons


def _refresh_onboarding(settings: "Settings", skill_run: "SkillRun") -> Any:
    """Пересчитать ``OnboardingState`` на момент рендера отчёта."""
    import logging

    from alla.orchestrator import build_onboarding_state
    from alla.services.kb_lookup_service import lookup_kb_for_clusters

    logger = logging.getLogger(__name__)

    kb_entries: list[Any] = []
    if settings.kb_active:
        try:
            fresh = lookup_kb_for_clusters(
                skill_run.triage_report,
                None,
                settings,
            )
            kb_entries = fresh.kb_entries
        except Exception as exc:
            logger.warning(
                "Не удалось обновить KB entries для onboarding: %s. "
                "Используются кэшированные данные skill_run.",
                exc,
            )
            return skill_run.onboarding

    return build_onboarding_state(
        settings,
        skill_run.project_id,
        skill_run.clustering_report,
        kb_entries=kb_entries,
    )


def build_analysis_result(
    skill_run: "SkillRun", settings: "Settings"
) -> "AnalysisResult":
    """Собрать :class:`AnalysisResult` из ``SkillRun`` для рендера HTML."""
    from alla.orchestrator import AnalysisResult
    from alla.services.agent_analysis_adapter import (
        agent_to_launch_summary,
        agent_to_llm_result,
    )

    llm_result = None
    llm_summary = None
    if skill_run.agent_analysis is not None and skill_run.clustering_report is not None:
        llm_result = agent_to_llm_result(
            skill_run.agent_analysis,
            skill_run.clustering_report,
        )
        llm_summary = agent_to_launch_summary(skill_run.agent_analysis)

    onboarding = _refresh_onboarding(settings, skill_run)

    return AnalysisResult(
        triage_report=skill_run.triage_report,
        clustering_report=skill_run.clustering_report,
        kb_results=skill_run.kb_results,
        kb_provenance=skill_run.kb_provenance,
        llm_result=llm_result,
        llm_launch_summary=llm_summary,
        feedback_contexts=skill_run.feedback_contexts,
        onboarding=onboarding,
    )
