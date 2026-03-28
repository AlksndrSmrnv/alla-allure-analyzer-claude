"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from alla.clients.base import TestResultsProvider, TestResultsUpdater
from alla.config import Settings
from alla.knowledge.feedback_models import (
    FeedbackClusterContext,
    FeedbackRecord,
    FeedbackVote,
)
from alla.knowledge.feedback_signature import (
    build_feedback_cluster_context,
    get_cluster_feedback_sources,
)
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.onboarding import OnboardingMode, OnboardingState
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.kb_push_service import KBPushResult
from alla.utils.step_paths import are_step_paths_compatible, normalize_step_path
from alla.utils.text_preview import preview_head

if TYPE_CHECKING:
    from alla.knowledge.base import KnowledgeBaseProvider
    from alla.knowledge.feedback_store import FeedbackStore
    from alla.knowledge.matcher import MatcherConfig
    from alla.knowledge.models import KBEntry
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, LLMPushResult

logger = logging.getLogger(__name__)
_KB_QUERY_LOG_PREVIEW_CHARS = 220


@dataclass
class AnalysisResult:
    """Полный результат анализа запуска."""

    triage_report: TriageReport
    clustering_report: ClusteringReport | None = None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_push_result: KBPushResult | None = None
    kb_provenance: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    llm_result: LLMAnalysisResult | None = None
    llm_push_result: LLMPushResult | None = None
    llm_launch_summary: LLMLaunchSummary | None = None
    feedback_contexts: dict[str, FeedbackClusterContext] = field(default_factory=dict)
    onboarding: OnboardingState = field(default_factory=OnboardingState)


@dataclass
class _KBStageResult:
    """Результат KB stage, агрегированный в одном объекте."""

    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_entries: list["KBEntry"] = field(default_factory=list)
    kb_provenance: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    feedback_contexts: dict[str, FeedbackClusterContext] = field(default_factory=dict)


@dataclass(frozen=True)
class _ClusterKBLookup:
    """Результат KB lookup для одного кластера."""

    matches: list[KBMatchResult]
    provenance: tuple[int, int, int]
    feedback_context: FeedbackClusterContext | None


async def analyze_launch(
    launch_id: int,
    client: TestResultsProvider,
    settings: Settings,
    *,
    updater: TestResultsUpdater | None = None,
) -> AnalysisResult:
    """Запустить полный pipeline анализа для одного запуска.

    Цепочка: триаж → логи → кластеризация → KB-поиск → LLM-анализ → LLM-push → KB-push (fallback).
    """
    from alla.services.triage_service import TriageService

    service = TriageService(client, settings)
    report = await service.analyze_launch(launch_id)
    await _enrich_with_logs(report, client, settings)

    clustering_report = _cluster_failures(launch_id, report, settings)
    kb_stage = _run_kb_stage(report, clustering_report, settings)
    onboarding = _build_onboarding_state(
        settings,
        report.project_id,
        clustering_report,
        kb_entries=kb_stage.kb_entries,
    )

    llm_result, llm_launch_summary = await _run_llm_stage(
        report,
        clustering_report,
        settings,
        kb_stage=kb_stage,
    )
    llm_push_result = await _push_llm_stage(
        report,
        clustering_report,
        llm_result,
        updater,
        settings,
    )
    kb_push_result = await _push_kb_stage(
        report,
        clustering_report,
        kb_stage,
        llm_result,
        updater,
        settings,
    )

    return AnalysisResult(
        triage_report=report,
        clustering_report=clustering_report,
        kb_results=kb_stage.kb_results,
        kb_push_result=kb_push_result,
        kb_provenance=kb_stage.kb_provenance,
        llm_result=llm_result,
        llm_push_result=llm_push_result,
        llm_launch_summary=llm_launch_summary,
        feedback_contexts=kb_stage.feedback_contexts,
        onboarding=onboarding,
    )


async def _enrich_with_logs(
    report: TriageReport,
    client: TestResultsProvider,
    settings: Settings,
) -> None:
    """Обогатить failed tests логами из аттачментов, если клиент это поддерживает."""
    if not report.failed_tests:
        return

    from alla.clients.base import AttachmentProvider
    from alla.services.log_extraction_service import (
        LogExtractionConfig,
        LogExtractionService,
    )

    if not isinstance(client, AttachmentProvider):
        logger.debug(
            "Провайдер не реализует AttachmentProvider. "
            "Логи из аттачментов пропущены."
        )
        return

    log_service = LogExtractionService(
        client,
        LogExtractionConfig(concurrency=settings.logs_concurrency),
    )
    try:
        await log_service.enrich_with_logs(report.failed_tests)
    except Exception as exc:
        logger.warning("Log enrichment: ошибка: %s", exc)


def _cluster_failures(
    launch_id: int,
    report: TriageReport,
    settings: Settings,
) -> ClusteringReport | None:
    """Построить clustering report для активных падений запуска."""
    if not report.failed_tests:
        return None

    from alla.services.clustering_service import ClusteringConfig, ClusteringService

    clustering_service = ClusteringService(
        ClusteringConfig(
            similarity_threshold=settings.clustering_threshold,
            log_similarity_weight=settings.logs_clustering_weight,
        )
    )
    return clustering_service.cluster_failures(launch_id, report.failed_tests)


def _run_kb_stage(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    settings: Settings,
) -> _KBStageResult:
    """Выполнить KB search stage и собрать его артефакты."""
    if not settings.kb_active:
        return _KBStageResult()

    from alla.knowledge.matcher import MatcherConfig

    matcher_config = MatcherConfig(
        min_score=settings.kb_min_score,
        max_results=settings.kb_max_results,
    )
    feedback_store = _get_feedback_store(
        kb_postgres_dsn=settings.kb_postgres_dsn,
    )
    kb = _get_or_create_kb(
        matcher_config,
        report.project_id,
        kb_postgres_dsn=settings.kb_postgres_dsn,
    )

    kb_stage = _KBStageResult(kb_entries=kb.get_all_entries())
    if clustering_report is None:
        return kb_stage

    entries_by_entry_id = {
        entry.entry_id: entry
        for entry in kb_stage.kb_entries
        if entry.entry_id is not None
    }
    test_by_id = _index_failed_tests(report.failed_tests)

    for cluster in clustering_report.clusters:
        try:
            lookup = _lookup_cluster_kb(
                cluster,
                kb,
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

        kb_stage.kb_provenance[cluster.cluster_id] = lookup.provenance
        if lookup.feedback_context is not None:
            kb_stage.feedback_contexts[cluster.cluster_id] = lookup.feedback_context
        if lookup.matches:
            kb_stage.kb_results[cluster.cluster_id] = lookup.matches

    return kb_stage


def _lookup_cluster_kb(
    cluster: FailureCluster,
    kb: KnowledgeBaseProvider,
    feedback_store: FeedbackStore,
    entries_by_entry_id: dict[int, "KBEntry"],
    test_by_id: dict[int, FailedTestSummary],
    settings: Settings,
) -> _ClusterKBLookup:
    """Найти KB-совпадения для одного кластера с exact-feedback rerank."""
    query_text, message_len, trace_len, log_len = _build_kb_query_text(
        cluster,
        test_by_id,
        include_trace=False,
    )
    provenance = (message_len, trace_len, log_len)
    feedback_context = build_feedback_cluster_context(cluster, test_by_id)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "KB query [%s]: rep_test_id=%s, combined_len=%d (msg=%d, trace=%d, log=%d)",
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
        base_exact_feedback = feedback_store.get_feedback_for_signature(
            feedback_context.base_issue_signature.signature_hash,
            feedback_context.base_issue_signature.version,
        )
        step_exact_feedback = (
            feedback_store.get_feedback_for_signature(
                feedback_context.step_issue_signature.signature_hash,
                feedback_context.step_issue_signature.version,
            )
            if feedback_context.step_issue_signature is not None
            else []
        )
        matches = _apply_exact_feedback_memory(
            matches,
            base_exact_feedback,
            entries_by_entry_id,
            step_exact_feedback=step_exact_feedback,
            query_step_path=cluster.example_step_path,
            max_results=settings.kb_max_results,
        )

    return _ClusterKBLookup(
        matches=matches,
        provenance=provenance,
        feedback_context=feedback_context,
    )


async def _run_llm_stage(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    settings: Settings,
    *,
    kb_stage: _KBStageResult,
) -> tuple["LLMAnalysisResult | None", "LLMLaunchSummary | None"]:
    """Выполнить LLM stage для кластеров и summary по прогону."""
    if (
        not settings.llm_active
        or clustering_report is None
        or not clustering_report.clusters
    ):
        return None, None

    from alla.clients.langflow_client import LangflowClient
    from alla.services.llm_service import LLMService

    _log_llm_stage_input(clustering_report, report.failed_tests, kb_stage.kb_results)

    llm_result = None
    async with LangflowClient(
        base_url=settings.langflow_base_url,
        flow_id=settings.langflow_flow_id,
        api_key=settings.langflow_api_key,
        timeout=settings.llm_timeout,
        ssl_verify=settings.ssl_verify,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
    ) as langflow:
        llm_service = LLMService(
            langflow,
            concurrency=settings.llm_concurrency,
        )
        try:
            llm_result = await llm_service.analyze_clusters(
                clustering_report,
                kb_results=kb_stage.kb_results or None,
                failed_tests=report.failed_tests,
                kb_provenance=kb_stage.kb_provenance or None,
            )
        except Exception as exc:
            logger.warning("LLM анализ: ошибка: %s", exc)

        llm_launch_summary = await llm_service.generate_launch_summary(
            clustering_report,
            report,
            llm_result,
        )

    return llm_result, llm_launch_summary


def _log_llm_stage_input(
    clustering_report: ClusteringReport,
    failed_tests: list[FailedTestSummary],
    kb_results: dict[str, list[KBMatchResult]],
) -> None:
    """Залогировать суммарный объём данных перед LLM stage."""
    test_by_id = _index_failed_tests(failed_tests)
    clusters_with_logs = 0
    clusters_with_kb = 0

    for cluster in clustering_report.clusters:
        if kb_results.get(cluster.cluster_id):
            clusters_with_kb += 1
        if cluster.representative_test_id is None:
            continue
        representative = test_by_id.get(cluster.representative_test_id)
        if representative is not None and representative.log_snippet:
            clusters_with_logs += 1

    logger.info(
        "LLM: отправка %d кластеров на анализ (с логами: %d, с KB: %d)",
        len(clustering_report.clusters),
        clusters_with_logs,
        clusters_with_kb,
    )


async def _push_llm_stage(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    llm_result: "LLMAnalysisResult | None",
    updater: TestResultsUpdater | None,
    settings: Settings,
) -> "LLMPushResult | None":
    """Записать LLM-рекомендации в TestOps, если stage дал результат."""
    if (
        not settings.push_to_testops
        or not settings.llm_active
        or clustering_report is None
        or llm_result is None
        or llm_result.analyzed_count <= 0
        or updater is None
    ):
        return None

    from alla.services.llm_service import push_llm_results

    try:
        return await push_llm_results(
            clustering_report,
            llm_result,
            report,
            updater,
            concurrency=settings.detail_concurrency,
        )
    except Exception as exc:
        logger.warning("LLM push: ошибка при записи рекомендаций: %s", exc)
        return None


async def _push_kb_stage(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    kb_stage: _KBStageResult,
    llm_result: "LLMAnalysisResult | None",
    updater: TestResultsUpdater | None,
    settings: Settings,
) -> KBPushResult | None:
    """Fallback KB push, когда LLM не сработал или отключён."""
    llm_succeeded = llm_result is not None and llm_result.analyzed_count > 0
    if (
        not settings.push_to_testops
        or not settings.kb_active
        or llm_succeeded
        or not kb_stage.kb_results
        or clustering_report is None
        or updater is None
    ):
        return None

    from alla.services.kb_push_service import KBPushService

    push_service = KBPushService(
        updater,
        concurrency=settings.detail_concurrency,
    )
    try:
        return await push_service.push_kb_results(
            clustering_report,
            kb_stage.kb_results,
            report,
        )
    except Exception as exc:
        logger.warning("KB push: ошибка при записи рекомендаций: %s", exc)
        return None


def _index_failed_tests(
    failed_tests: list[FailedTestSummary],
) -> dict[int, FailedTestSummary]:
    """Построить индекс test_result_id -> FailedTestSummary."""
    return {test.test_result_id: test for test in failed_tests}


def _build_onboarding_state(
    settings: Settings,
    project_id: int | None,
    clustering_report: ClusteringReport | None,
    *,
    kb_entries: list["KBEntry"] | None = None,
) -> OnboardingState:
    """Определить onboarding state проекта для JSON и HTML-отчёта."""
    prioritized_cluster_ids = _prioritize_cluster_ids(clustering_report)

    if not settings.kb_active:
        return OnboardingState(
            mode=OnboardingMode.KB_NOT_CONFIGURED,
            prioritized_cluster_ids=prioritized_cluster_ids,
        )

    entries = kb_entries or []
    starter_pack_available = any(entry.project_id is None for entry in entries)
    project_kb_entries = 0
    if project_id is not None:
        project_kb_entries = sum(
            1 for entry in entries if entry.project_id == project_id
        )

    guided = project_id is not None and project_kb_entries == 0
    return OnboardingState(
        mode=OnboardingMode.GUIDED if guided else OnboardingMode.NORMAL,
        needs_bootstrap=guided,
        project_kb_entries=project_kb_entries,
        prioritized_cluster_ids=prioritized_cluster_ids,
        starter_pack_available=starter_pack_available,
    )


def _prioritize_cluster_ids(
    clustering_report: ClusteringReport | None,
    *,
    limit: int = 3,
) -> list[str]:
    """Вернуть top-N cluster_id для guided onboarding."""
    if clustering_report is None or not clustering_report.clusters or limit <= 0:
        return []

    ranked = sorted(
        enumerate(clustering_report.clusters),
        key=lambda item: (-item[1].member_count, item[0]),
    )
    return [cluster.cluster_id for _, cluster in ranked[:limit]]


def _build_kb_query_text(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    include_trace: bool = True,
) -> tuple[str, int, int, int]:
    """Собрать единый текст запроса для KB: message + log или message + trace.

    Если у кластера есть application log, query строится как ``message + log``.
    Trace используется только fallback'ом, когда лог отсутствует. Лог и trace
    взаимоисключающие в KB query, чтобы сохранить exact substring match (Tier 1)
    с KB-записями, созданными из report form (error_example = message + log).
    """
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


def _apply_exact_feedback_memory(
    matches: list[KBMatchResult],
    base_exact_feedback: list[FeedbackRecord],
    entries_by_entry_id: dict[int, "KBEntry"],
    *,
    step_exact_feedback: list[FeedbackRecord] | None = None,
    query_step_path: str | None = None,
    max_results: int,
) -> list[KBMatchResult]:
    """Применить exact feedback memory поверх обычных KB text matches."""
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
    candidate_entry_ids = set(base_records_by_entry_id) | set(step_records_by_entry_id)

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
            and not are_step_paths_compatible(entry.step_path, normalized_query_step_path)
        ):
            result_by_entry_id.pop(entry_id, None)
            continue

        record = (
            step_records_by_entry_id.get(entry_id) or base_records_by_entry_id.get(entry_id)
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
            "Feedback memory: exact step-aware issue signature was confirmed previously"
            if entry.step_path and entry_id in step_records_by_entry_id
            else "Feedback memory: exact issue signature was confirmed previously"
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
def _get_or_create_kb(
    matcher_config: "MatcherConfig",
    project_id: int | None = None,
    *,
    kb_postgres_dsn: str = "",
) -> KnowledgeBaseProvider:
    """Создать новый экземпляр KB (PostgreSQL бэкенд).

    Каждый вызов создаёт свежий экземпляр, чтобы подхватывать
    изменения в таблице alla.kb_entry без перезапуска сервера.
    """
    from alla.knowledge.postgres_kb import PostgresKnowledgeBase

    kb = PostgresKnowledgeBase(
        dsn=kb_postgres_dsn,
        matcher_config=matcher_config,
        project_id=project_id,
    )

    logger.debug("KB: создан новый экземпляр (postgres)")
    return kb


def _get_feedback_store(*, kb_postgres_dsn: str) -> FeedbackStore:
    """Создать exact feedback store для текущего запуска анализа."""
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

    return PostgresFeedbackStore(dsn=kb_postgres_dsn)
