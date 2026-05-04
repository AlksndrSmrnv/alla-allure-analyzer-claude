"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from alla.clients.base import TestResultsProvider, TestResultsUpdater
from alla.config import Settings
from alla.knowledge.feedback_models import FeedbackClusterContext
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport
from alla.models.onboarding import OnboardingMode, OnboardingState
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.kb_lookup_service import KBStageResult as _KBStageResult
from alla.services.kb_lookup_service import lookup_kb_for_clusters
from alla.services.kb_push_service import KBPushResult

if TYPE_CHECKING:
    from alla.knowledge.merge_rules_store import PostgresMergeRulesStore
    from alla.knowledge.models import KBEntry
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, LLMPushResult

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Полный результат анализа запуска."""

    triage_report: TriageReport
    clustering_report: ClusteringReport | None = None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_push_result: KBPushResult | None = None
    kb_provenance: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    llm_result: "LLMAnalysisResult | None" = None
    llm_push_result: "LLMPushResult | None" = None
    llm_launch_summary: "LLMLaunchSummary | None" = None
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
    """
    from alla.services.triage_service import TriageService

    service = TriageService(client, settings)
    report = await service.analyze_launch(launch_id)
    await _enrich_with_logs(report, client, settings)

    clustering_report = _cluster_failures(launch_id, report, settings)
    clustering_report = _apply_merge_rules_phase(
        report,
        clustering_report,
        settings,
    )
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


def _apply_merge_rules_phase(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    settings: Settings,
) -> ClusteringReport | None:
    """Применить сохранённые merge rules к результату кластеризации."""
    if (
        clustering_report is None
        or not clustering_report.clusters
        or report.project_id is None
        or not settings.kb_active
    ):
        return clustering_report

    merge_rules_store = _get_merge_rules_store(
        kb_postgres_dsn=settings.kb_postgres_dsn,
    )
    try:
        rules = merge_rules_store.load_rules(report.project_id)
    except Exception as exc:
        logger.warning(
            "Merge rules: не удалось загрузить правила для project_id=%s: %s",
            report.project_id,
            exc,
        )
        return clustering_report

    if not rules:
        return clustering_report

    from alla.services.merge_service import apply_merge_rules

    try:
        merged_report = apply_merge_rules(
            clustering_report,
            report.failed_tests,
            rules,
        )
    except Exception as exc:
        logger.warning("Merge rules: ошибка применения: %s", exc)
        return clustering_report

    if merged_report.cluster_count != clustering_report.cluster_count:
        logger.info(
            "Merge rules: project_id=%s, кластеров %d -> %d",
            report.project_id,
            clustering_report.cluster_count,
            merged_report.cluster_count,
        )
    return merged_report


def _run_kb_stage(
    report: TriageReport,
    clustering_report: ClusteringReport | None,
    settings: Settings,
) -> _KBStageResult:
    """Выполнить KB search stage и собрать его артефакты.

    Тонкий wrapper поверх :func:`alla.services.kb_lookup_service.lookup_kb_for_clusters`
    — один источник истины для server-side и skill-режима.
    """
    return lookup_kb_for_clusters(report, clustering_report, settings)


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

    _log_llm_stage_input(clustering_report, report.failed_tests, kb_stage.kb_results)

    import os

    cert_path: str | None = None
    key_path: str | None = None
    gigachat: object | None = None

    try:
        from alla.clients.gigachat_client import GigaChatClient
        from alla.models.llm import LLMLaunchSummary
        from alla.services.llm_service import LLMService

        try:
            cert_path, key_path = settings.resolve_cert_files()
            gigachat = GigaChatClient(
                base_url=settings.gigachat_base_url,
                cert_file=cert_path,
                key_file=key_path,
                model=settings.gigachat_model,
                verify_ssl=settings.ssl_verify,
                timeout=settings.llm_timeout,
                max_retries=settings.llm_max_retries,
                retry_base_delay=settings.llm_retry_base_delay,
            )
        except Exception as exc:
            logger.warning("LLM stage пропущен: не удалось подготовить GigaChat: %s", exc)
            return None, None

        llm_service = LLMService(
            gigachat,
            concurrency=settings.llm_concurrency,
            request_delay=settings.llm_request_delay,
            message_max_chars=settings.llm_prompt_message_max_chars,
            trace_max_chars=settings.llm_prompt_trace_max_chars,
            log_max_chars=settings.llm_prompt_log_max_chars,
        )
        llm_result = None
        try:
            llm_result = await llm_service.analyze_clusters(
                clustering_report,
                kb_results=kb_stage.kb_results or None,
                failed_tests=report.failed_tests,
                kb_provenance=kb_stage.kb_provenance or None,
            )
        except Exception as exc:
            logger.warning("LLM анализ: ошибка: %s", exc)

        try:
            llm_launch_summary = await llm_service.generate_launch_summary(
                clustering_report,
                report,
                llm_result,
            )
        except Exception as exc:
            logger.warning("LLM summary: ошибка подготовки: %s", exc)
            llm_launch_summary = LLMLaunchSummary(summary_text="", error=str(exc))

        # Суммарный расход токенов (cluster analysis + launch summary)
        clusters_usage = llm_result.token_usage if llm_result else None
        summary_usage = llm_launch_summary.token_usage if llm_launch_summary else None
        total_prompt = (clusters_usage.prompt_tokens if clusters_usage else 0) + (summary_usage.prompt_tokens if summary_usage else 0)
        total_completion = (clusters_usage.completion_tokens if clusters_usage else 0) + (summary_usage.completion_tokens if summary_usage else 0)
        total_all = (clusters_usage.total_tokens if clusters_usage else 0) + (summary_usage.total_tokens if summary_usage else 0)
        if total_all > 0:
            logger.info(
                "LLM: токены — входящих: %d, исходящих: %d, всего: %d",
                total_prompt, total_completion, total_all,
            )

        return llm_result, llm_launch_summary
    finally:
        if gigachat is not None:
            try:
                await gigachat.close()
            except Exception as exc:
                logger.warning("LLM stage: ошибка закрытия GigaChat client: %s", exc)
        for path in (cert_path, key_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def _log_llm_stage_input(
    clustering_report: ClusteringReport,
    failed_tests: list[FailedTestSummary],
    kb_results: dict[str, list[KBMatchResult]],
) -> None:
    """Залогировать суммарный объём данных перед LLM stage."""
    test_by_id = {test.test_result_id: test for test in failed_tests}
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


def _get_merge_rules_store(*, kb_postgres_dsn: str) -> "PostgresMergeRulesStore":
    """Создать merge rules store для текущего запуска анализа."""
    from alla.knowledge.merge_rules_store import PostgresMergeRulesStore

    return PostgresMergeRulesStore(dsn=kb_postgres_dsn)
