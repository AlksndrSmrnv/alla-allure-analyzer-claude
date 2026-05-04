"""Публичный сервис KB-поиска и применения exact feedback memory.

Раньше всё это жило приватно внутри :mod:`alla.services.llm_service`-
смежного :mod:`alla.orchestrator`. Сейчас вынесено в стабильную
публичную точку входа, чтобы её одинаково импортировали:

* server-side путь (через :func:`alla.orchestrator.analyze_launch`);
* skill-скрипт ``alla-skill/scripts/fetch_clusters.py``.

Главная функция — :func:`lookup_kb_for_clusters`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alla.config import Settings
from alla.knowledge.feedback_models import (
    FeedbackClusterContext,
    FeedbackRecord,
    FeedbackVote,
)
from alla.knowledge.feedback_signature import build_feedback_cluster_context
from alla.knowledge.matcher import MatcherConfig
from alla.knowledge.models import KBEntry, KBMatchResult
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.testops import FailedTestSummary, TriageReport
from alla.utils.step_paths import are_step_paths_compatible, normalize_step_path
from alla.utils.text_preview import preview_head

if TYPE_CHECKING:
    from alla.knowledge.base import KnowledgeBaseProvider
    from alla.knowledge.feedback_store import FeedbackStore

logger = logging.getLogger(__name__)

__all__ = [
    "KBStageResult",
    "lookup_kb_for_clusters",
    "build_kb_query_text",
]

_KB_QUERY_LOG_PREVIEW_CHARS = 220


@dataclass
class KBStageResult:
    """Результат KB stage, агрегированный в одном объекте.

    Поля:
        kb_results: cluster_id → отсортированный список совпадений.
        kb_entries: все записи KB, загруженные для текущего проекта.
        kb_provenance: cluster_id → ``(message_chars, trace_chars, log_chars)``
            — длины частей запроса, по которому искалось совпадение.
        feedback_contexts: cluster_id → :class:`FeedbackClusterContext`,
            нужен HTML-отчёту и feedback API.
    """

    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_entries: list[KBEntry] = field(default_factory=list)
    kb_provenance: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    feedback_contexts: dict[str, FeedbackClusterContext] = field(default_factory=dict)


@dataclass(frozen=True)
class _ClusterKBLookup:
    matches: list[KBMatchResult]
    provenance: tuple[int, int, int]
    feedback_context: FeedbackClusterContext | None


def lookup_kb_for_clusters(
    triage_report: TriageReport,
    clustering_report: ClusteringReport | None,
    settings: Settings,
    *,
    kb_provider: "KnowledgeBaseProvider | None" = None,
    feedback_store: "FeedbackStore | None" = None,
) -> KBStageResult:
    """Прогнать KB-поиск и exact-feedback rerank по всем кластерам.

    Если ``settings.kb_active`` ложно — возвращает пустой
    :class:`KBStageResult` (KB не подключена). Иначе создаёт временные
    провайдеры (или использует переданные) и для каждого кластера
    выполняет text-search + exact-feedback rerank.
    """
    if not settings.kb_active:
        return KBStageResult()

    if kb_provider is None:
        kb_provider = _build_kb_provider(settings, triage_report.project_id)
    if feedback_store is None:
        feedback_store = _build_feedback_store(settings)

    stage = KBStageResult(kb_entries=kb_provider.get_all_entries())
    if clustering_report is None or not clustering_report.clusters:
        return stage

    entries_by_entry_id = {
        entry.entry_id: entry
        for entry in stage.kb_entries
        if entry.entry_id is not None
    }
    test_by_id = _index_failed_tests(triage_report.failed_tests)

    for cluster in clustering_report.clusters:
        try:
            lookup = _lookup_single_cluster(
                cluster,
                kb_provider,
                feedback_store,
                entries_by_entry_id,
                test_by_id,
                settings,
            )
        except Exception as exc:
            logger.warning(
                "Ошибка KB-поиска для кластера %s: %s",
                cluster.cluster_id,
                exc,
            )
            continue

        stage.kb_provenance[cluster.cluster_id] = lookup.provenance
        if lookup.feedback_context is not None:
            stage.feedback_contexts[cluster.cluster_id] = lookup.feedback_context
        if lookup.matches:
            stage.kb_results[cluster.cluster_id] = lookup.matches

    return stage


def build_kb_query_text(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    include_trace: bool = True,
) -> tuple[str, int, int, int]:
    """Собрать единый текст запроса для KB: ``message + log`` (или ``message + trace``).

    Возвращает ``(query_text, message_chars, trace_chars, log_chars)``.
    Если у кластера есть application log — query это ``message + log``;
    trace включается только как fallback при отсутствии лога. Это
    сохраняет exact substring match (Tier 1) с записями KB, которые
    обычно создаются из ``message + log``.
    """
    from alla.knowledge.feedback_signature import get_cluster_feedback_sources

    message, trace, log_snippet = get_cluster_feedback_sources(cluster, test_by_id)
    if not include_trace:
        trace = ""

    effective_trace = ""
    parts: list[str] = []
    if message:
        parts.append(message)
    if log_snippet:
        parts.append(log_snippet)
    elif trace:
        effective_trace = trace
        parts.append(trace)

    return "\n".join(parts), len(message), len(effective_trace), len(log_snippet)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _index_failed_tests(
    failed_tests: list[FailedTestSummary],
) -> dict[int, FailedTestSummary]:
    return {test.test_result_id: test for test in failed_tests}


def _build_kb_provider(
    settings: Settings, project_id: int | None
) -> "KnowledgeBaseProvider":
    from alla.knowledge.postgres_kb import PostgresKnowledgeBase

    matcher_config = MatcherConfig(
        min_score=settings.kb_min_score,
        max_results=settings.kb_max_results,
    )
    kb = PostgresKnowledgeBase(
        dsn=settings.kb_postgres_dsn,
        matcher_config=matcher_config,
        project_id=project_id,
    )
    logger.debug("KB lookup: создан новый PostgresKnowledgeBase")
    return kb


def _build_feedback_store(settings: Settings) -> "FeedbackStore":
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

    return PostgresFeedbackStore(dsn=settings.kb_postgres_dsn)


def _lookup_single_cluster(
    cluster: FailureCluster,
    kb: "KnowledgeBaseProvider",
    feedback_store: "FeedbackStore",
    entries_by_entry_id: dict[int, KBEntry],
    test_by_id: dict[int, FailedTestSummary],
    settings: Settings,
) -> _ClusterKBLookup:
    query_text, message_len, trace_len, log_len = build_kb_query_text(
        cluster,
        test_by_id,
        include_trace=False,
    )
    provenance = (message_len, trace_len, log_len)
    feedback_context = build_feedback_cluster_context(cluster, test_by_id)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "KB query [%s]: rep_test_id=%s, combined_len=%d "
            "(msg=%d, trace=%d, log=%d)",
            cluster.cluster_id,
            cluster.representative_test_id,
            len(query_text),
            message_len,
            trace_len,
            log_len,
        )
        if query_text.strip():
            logger.debug(
                "KB query [%s]: head='%s'",
                cluster.cluster_id,
                preview_head(query_text, _KB_QUERY_LOG_PREVIEW_CHARS),
            )

    matches = (
        kb.search_by_error(
            query_text,
            query_label=f"{cluster.cluster_id}:combined",
            query_step_path=cluster.example_step_path,
        )
        if query_text.strip()
        else []
    )

    if feedback_context is not None:
        base_records = feedback_store.get_feedback_for_signature(
            feedback_context.base_issue_signature.signature_hash,
            feedback_context.base_issue_signature.version,
        )
        step_records = (
            feedback_store.get_feedback_for_signature(
                feedback_context.step_issue_signature.signature_hash,
                feedback_context.step_issue_signature.version,
            )
            if feedback_context.step_issue_signature is not None
            else []
        )
        matches = _apply_exact_feedback_memory(
            matches,
            base_records,
            entries_by_entry_id,
            step_exact_feedback=step_records,
            query_step_path=cluster.example_step_path,
            max_results=settings.kb_max_results,
        )

    return _ClusterKBLookup(
        matches=matches,
        provenance=provenance,
        feedback_context=feedback_context,
    )


def _apply_exact_feedback_memory(
    matches: list[KBMatchResult],
    base_exact_feedback: list[FeedbackRecord],
    entries_by_entry_id: dict[int, KBEntry],
    *,
    step_exact_feedback: list[FeedbackRecord] | None = None,
    query_step_path: str | None = None,
    max_results: int,
) -> list[KBMatchResult]:
    result_by_entry_id: dict[int, KBMatchResult] = {}
    passthrough_matches: list[KBMatchResult] = []
    normalized_query_step_path = normalize_step_path(query_step_path)

    for match in matches:
        entry_id = match.entry.entry_id
        if entry_id is None:
            passthrough_matches.append(match)
            continue
        result_by_entry_id[entry_id] = match

    base_records_by_entry_id = {
        record.kb_entry_id: record for record in base_exact_feedback
    }
    step_records_by_entry_id = {
        record.kb_entry_id: record for record in (step_exact_feedback or [])
    }
    candidate_entry_ids = (
        set(base_records_by_entry_id) | set(step_records_by_entry_id)
    )

    for entry_id in candidate_entry_ids:
        entry = entries_by_entry_id.get(entry_id)
        if entry is None:
            existing_match = result_by_entry_id.get(entry_id)
            entry = existing_match.entry if existing_match is not None else None
        if entry is None:
            continue
        if (
            entry.step_path
            and normalized_query_step_path
            and not are_step_paths_compatible(
                entry.step_path, normalized_query_step_path
            )
        ):
            result_by_entry_id.pop(entry_id, None)
            continue

        record = (
            step_records_by_entry_id.get(entry_id)
            or base_records_by_entry_id.get(entry_id)
            if entry.step_path
            else base_records_by_entry_id.get(entry_id)
        )
        if record is None:
            continue

        entry_id = record.kb_entry_id
        if record.vote == FeedbackVote.DISLIKE:
            result_by_entry_id.pop(entry_id, None)
            continue

        effective_match = result_by_entry_id.get(entry_id)
        if effective_match is None:
            entry = entries_by_entry_id.get(entry_id)
            if entry is None:
                continue
            effective_match = KBMatchResult(
                entry=entry,
                score=1.0,
                matched_on=[],
                match_origin="feedback_exact",
            )
            result_by_entry_id[entry_id] = effective_match

        reason = (
            "Feedback memory: exact step-aware issue signature was "
            "confirmed previously"
            if entry.step_path and entry_id in step_records_by_entry_id
            else "Feedback memory: exact issue signature was "
            "confirmed previously"
        )
        effective_match.score = 1.0
        effective_match.match_origin = "feedback_exact"
        effective_match.feedback_vote = record.vote.value
        effective_match.feedback_id = record.feedback_id
        effective_match.matched_on = [reason] + [
            item for item in effective_match.matched_on if item != reason
        ]

    merged = list(result_by_entry_id.values()) + passthrough_matches
    merged.sort(
        key=lambda item: (
            0 if item.match_origin == "feedback_exact" else 1,
            -item.score,
            item.entry.title,
        ),
    )
    return merged[:max_results]
