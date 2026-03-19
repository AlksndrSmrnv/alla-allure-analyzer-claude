"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

from __future__ import annotations

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

if TYPE_CHECKING:
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


async def analyze_launch(
    launch_id: int,
    client: TestResultsProvider,
    settings: Settings,
    *,
    updater: TestResultsUpdater | None = None,
) -> AnalysisResult:
    """Запустить полный pipeline анализа для одного запуска.

    Цепочка: триаж → логи → кластеризация → KB-поиск → LLM-анализ → LLM-push → KB-push (fallback).

    Args:
        launch_id: ID запуска в Allure TestOps.
        client: Провайдер для чтения результатов тестов.
        settings: Настройки приложения.
        updater: Провайдер для записи (комментарии). Если None — KB push
            не выполняется вне зависимости от настроек.

    Returns:
        AnalysisResult с результатами всех этапов.

    Raises:
        AllaError: При ошибках API, аутентификации, пагинации.
        KnowledgeBaseError: При ошибке инициализации базы знаний.
    """
    from alla.services.triage_service import TriageService

    # 1. Триаж
    service = TriageService(client, settings)
    report = await service.analyze_launch(launch_id)

    # 1.5. Обогащение логами из аттачментов
    if report.failed_tests:
        from alla.clients.base import AttachmentProvider
        from alla.services.log_extraction_service import (
            LogExtractionConfig,
            LogExtractionService,
        )

        if isinstance(client, AttachmentProvider):
            log_config = LogExtractionConfig(
                concurrency=settings.logs_concurrency,
            )
            log_service = LogExtractionService(client, log_config)
            try:
                await log_service.enrich_with_logs(report.failed_tests)
            except Exception as exc:
                logger.warning("Log enrichment: ошибка: %s", exc)
        else:
            logger.debug(
                "Провайдер не реализует AttachmentProvider. "
                "Логи из аттачментов пропущены."
            )

    clustering_report = None
    kb_results: dict[str, list[KBMatchResult]] = {}
    kb_push_result = None
    kb_provenance: dict[str, tuple[int, int, int]] = {}
    feedback_contexts: dict[str, FeedbackClusterContext] = {}
    onboarding = OnboardingState(
        mode=OnboardingMode.KB_NOT_CONFIGURED
        if not settings.kb_active
        else OnboardingMode.NORMAL,
        prioritized_cluster_ids=_prioritize_cluster_ids(None),
    )

    # 2. Кластеризация
    if report.failed_tests:
        from alla.services.clustering_service import ClusteringConfig, ClusteringService

        clustering_kwargs: dict = {
            "similarity_threshold": settings.clustering_threshold,
        }
        if settings.logs_clustering_weight > 0:
            clustering_kwargs["log_similarity_weight"] = settings.logs_clustering_weight

        clustering_service = ClusteringService(
            ClusteringConfig(**clustering_kwargs)
        )
        clustering_report = clustering_service.cluster_failures(
            launch_id, report.failed_tests,
        )

    kb_entries: list["KBEntry"] = []

    # 3. Поиск по базе знаний
    if settings.kb_active:
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
        if hasattr(kb, "get_all_entries"):
            kb_entries = kb.get_all_entries()
        entries_by_entry_id = {
            entry.entry_id: entry
            for entry in kb_entries
            if entry.entry_id is not None
        }

        if clustering_report is not None:
            # Индекс test_result_id → FailedTestSummary для быстрого lookup
            test_by_id = {t.test_result_id: t for t in report.failed_tests}

            for cluster in clustering_report.clusters:
                try:
                    query_text, message_len, trace_len, log_len = _build_kb_query_text(
                        cluster, test_by_id,
                        include_trace=False,
                    )
                    kb_provenance[cluster.cluster_id] = (message_len, trace_len, log_len)

                    feedback_context = build_feedback_cluster_context(
                        cluster,
                        test_by_id,
                    )
                    if feedback_context is not None:
                        feedback_contexts[cluster.cluster_id] = feedback_context

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "KB query [%s]: rep_test_id=%s, "
                            "combined_len=%d (msg=%d, trace=%d, log=%d)",
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
                                _preview_head(query_text, _KB_QUERY_LOG_PREVIEW_CHARS),
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
                    if matches:
                        kb_results[cluster.cluster_id] = matches
                except Exception as exc:
                    logger.warning(
                        "Ошибка KB-поиска для кластера %s: %s",
                        cluster.cluster_id, exc,
                    )

    onboarding = _build_onboarding_state(
        settings,
        report.project_id,
        clustering_report,
        kb_entries=kb_entries,
    )

    # 4. LLM-анализ кластеров через Langflow
    llm_result = None
    llm_push_result = None
    llm_launch_summary = None

    if (
        settings.llm_active
        and clustering_report is not None
        and clustering_report.clusters
    ):
        from alla.clients.langflow_client import LangflowClient
        from alla.services.llm_service import LLMService

        # Суммарная статистика перед LLM-анализом
        _test_by_id_llm = (
            {t.test_result_id: t for t in report.failed_tests}
            if report.failed_tests
            else {}
        )
        clusters_with_logs = 0
        clusters_with_kb = 0
        for _c in clustering_report.clusters:
            if kb_results.get(_c.cluster_id):
                clusters_with_kb += 1
            if _test_by_id_llm and _c.representative_test_id is not None:
                _rep = _test_by_id_llm.get(_c.representative_test_id)
                if _rep and _rep.log_snippet:
                    clusters_with_logs += 1
        logger.info(
            "LLM: отправка %d кластеров на анализ "
            "(с логами: %d, с KB: %d)",
            len(clustering_report.clusters),
            clusters_with_logs,
            clusters_with_kb,
        )

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
                    kb_results=kb_results or None,
                    failed_tests=report.failed_tests,
                    kb_provenance=kb_provenance or None,
                )
            except Exception as exc:
                logger.warning("LLM анализ: ошибка: %s", exc)

            # Итоговый отчёт по всему прогону
            llm_launch_summary = await llm_service.generate_launch_summary(
                clustering_report,
                report,
                llm_result,
            )

    # 5. Запись LLM-рекомендаций в TestOps
    if (
        settings.push_to_testops
        and settings.llm_active
        and llm_result is not None
        and llm_result.analyzed_count > 0
        and updater is not None
    ):
        from alla.services.llm_service import push_llm_results

        try:
            llm_push_result = await push_llm_results(
                clustering_report,
                llm_result,
                report,
                updater,
                concurrency=settings.detail_concurrency,
            )
        except Exception as exc:
            logger.warning("LLM push: ошибка при записи рекомендаций: %s", exc)

    # 6. Fallback: запись рекомендаций KB в TestOps
    #    Выполняется только если LLM не включён или не дал успешных результатов.
    #    Когда LLM работает — KB-данные интегрируются в LLM-анализ (Stage 4),
    #    и дублирующий KB push не нужен.
    llm_succeeded = (
        llm_result is not None and llm_result.analyzed_count > 0
    )
    if (
        settings.push_to_testops
        and settings.kb_active
        and not llm_succeeded
        and kb_results
        and clustering_report is not None
        and updater is not None
    ):
        from alla.services.kb_push_service import KBPushService

        push_service = KBPushService(
            updater,
            concurrency=settings.detail_concurrency,
        )
        try:
            kb_push_result = await push_service.push_kb_results(
                clustering_report,
                kb_results,
                report,
            )
        except Exception as exc:
            logger.warning("KB push: ошибка при записи рекомендаций: %s", exc)

    return AnalysisResult(
        triage_report=report,
        clustering_report=clustering_report,
        kb_results=kb_results,
        kb_push_result=kb_push_result,
        kb_provenance=kb_provenance,
        llm_result=llm_result,
        llm_push_result=llm_push_result,
        llm_launch_summary=llm_launch_summary,
        feedback_contexts=feedback_contexts,
        onboarding=onboarding,
    )


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

    Args:
        cluster: Кластер падений.
        test_by_id: Словарь test_result_id → FailedTestSummary.
        include_trace: Разрешать ли fallback на Allure-trace при отсутствии лога.

    Returns:
        (combined_text, message_len, trace_len, log_len)
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
            match = result_by_entry_id.get(entry_id)
            entry = match.entry if match is not None else None
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

        match = result_by_entry_id.get(entry_id)
        if match is None:
            entry = entries_by_entry_id.get(entry_id)
            if entry is None:
                continue
            match = KBMatchResult(
                entry=entry,
                score=1.0,
                matched_on=[],
                match_origin="feedback_exact",
            )
            result_by_entry_id[entry_id] = match

        reason = (
            "Feedback memory: exact step-aware issue signature was confirmed previously"
            if entry.step_path and entry_id in step_records_by_entry_id
            else "Feedback memory: exact issue signature was confirmed previously"
        )
        match.score = 1.0
        match.match_origin = "feedback_exact"
        match.feedback_vote = record.vote.value
        match.feedback_id = record.feedback_id
        match.matched_on = [reason] + [
            item for item in match.matched_on if item != reason
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


def _preview_head(text: str, max_chars: int) -> str:
    """Сжать head-preview для DEBUG-логов одной строкой."""
    return text[:max_chars].replace("\n", " ")


def _preview_tail(text: str, max_chars: int) -> str:
    """Сжать tail-preview для DEBUG-логов одной строкой."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")


def _get_or_create_kb(
    matcher_config: object,
    project_id: int | None = None,
    *,
    kb_postgres_dsn: str = "",
) -> object:
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


def _get_feedback_store(*, kb_postgres_dsn: str):
    """Создать exact feedback store для текущего запуска анализа."""
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

    return PostgresFeedbackStore(dsn=kb_postgres_dsn)
