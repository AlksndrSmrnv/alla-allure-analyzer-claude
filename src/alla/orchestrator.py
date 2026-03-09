"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from alla.clients.base import TestResultsProvider, TestResultsUpdater
from alla.config import Settings
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.kb_push_service import KBPushResult

if TYPE_CHECKING:
    from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary, LLMPushResult

logger = logging.getLogger(__name__)
_KB_QUERY_LOG_PREVIEW_CHARS = 220

# Кэш KB: (kb_postgres_dsn, min_score, max_results, project_id) → экземпляр.
# Предотвращает повторное подключение к БД и re-fit TF-IDF на каждый запрос сервера.
_kb_cache: dict[tuple[str, float, int], object] = {}
_kb_cache_lock: object = None  # Ленивая инициализация asyncio.Lock


@dataclass
class AnalysisResult:
    """Полный результат анализа запуска."""

    triage_report: TriageReport
    clustering_report: ClusteringReport | None = None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_push_result: KBPushResult | None = None
    llm_result: LLMAnalysisResult | None = None
    llm_push_result: LLMPushResult | None = None
    llm_launch_summary: LLMLaunchSummary | None = None
    error_fingerprints: dict[str, str] = field(default_factory=dict)


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
    error_fingerprints: dict[str, str] = {}

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

    # 3. Поиск по базе знаний
    if settings.kb_active and clustering_report is not None:
        from alla.knowledge.matcher import MatcherConfig

        matcher_config = MatcherConfig(
            min_score=settings.kb_min_score,
            max_results=settings.kb_max_results,
        )
        kb = _get_or_create_kb(
            matcher_config,
            report.project_id,
            kb_postgres_dsn=settings.kb_postgres_dsn,
        )

        # Индекс test_result_id → FailedTestSummary для быстрого lookup
        test_by_id = {t.test_result_id: t for t in report.failed_tests}

        for cluster in clustering_report.clusters:
            try:
                query_text, message_len, trace_len, log_len = _build_kb_query_text(
                    cluster, test_by_id,
                    include_trace=True,
                )

                # Fingerprint из report_text (message + trace, без лога) —
                # лог варьируется между прогонами, fingerprint должен быть стабильным.
                fingerprint_text = _build_fingerprint_text(
                    cluster, test_by_id,
                    include_trace=True,
                )
                if fingerprint_text.strip():
                    from alla.utils.fingerprint import compute_fingerprint

                    fingerprint = compute_fingerprint(fingerprint_text)
                    error_fingerprints[cluster.cluster_id] = fingerprint

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

                fp = error_fingerprints.get(cluster.cluster_id)
                matches = (
                    kb.search_by_error(
                        query_text,
                        query_label=f"{cluster.cluster_id}:combined",
                        error_fingerprint=fp,
                    )
                    if query_text.strip()
                    else []
                )
                if matches:
                    kb_results[cluster.cluster_id] = matches
            except Exception as exc:
                logger.warning(
                    "Ошибка KB-поиска для кластера %s: %s",
                    cluster.cluster_id, exc,
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
        settings.llm_active
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
        llm_result is not None
        and (llm_result.analyzed_count - llm_result.kb_bypass_count) > 0
    )
    if (
        settings.kb_active
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
        llm_result=llm_result,
        llm_push_result=llm_push_result,
        llm_launch_summary=llm_launch_summary,
        error_fingerprints=error_fingerprints,
    )


def _build_kb_query_text(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    include_trace: bool = True,
) -> tuple[str, int, int, int]:
    """Собрать единый текст запроса для KB: message + trace + log_snippet.

    Объединяет данные из Allure-отчёта (message, trace) и логов приложения
    (log_snippet representative теста) в один документ. Это позволяет KB-записям,
    содержащим фрагменты и ошибки и лога, эффективно сопоставляться.

    Args:
        cluster: Кластер падений.
        test_by_id: Словарь test_result_id → FailedTestSummary.
        include_trace: Включать ли Allure-trace (stack trace) в query.

    Returns:
        (combined_text, message_len, trace_len, log_len)
    """
    representative = (
        test_by_id.get(cluster.representative_test_id)
        if cluster.representative_test_id is not None
        else None
    )

    message = (
        representative.status_message
        if representative and representative.status_message
        else cluster.example_message
    ) or ""
    trace = ""
    if include_trace:
        trace = (
            representative.status_trace
            if representative and representative.status_trace
            else cluster.example_trace_snippet
        ) or ""

    log_snippet = ""
    if representative and representative.log_snippet:
        log_snippet = representative.log_snippet.strip()
    if not log_snippet:
        for tid in cluster.member_test_ids:
            member = test_by_id.get(tid)
            if member and member.log_snippet and member.log_snippet.strip():
                log_snippet = member.log_snippet.strip()
                break

    parts = [part for part in (message, trace, log_snippet) if part]
    return "\n".join(parts), len(message), len(trace), len(log_snippet)


def _build_fingerprint_text(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    include_trace: bool = True,
) -> str:
    """Собрать текст для вычисления fingerprint (message + trace, без лога).

    Fingerprint должен быть стабильным между прогонами — логи варьируются,
    поэтому исключены.
    """
    representative = (
        test_by_id.get(cluster.representative_test_id)
        if cluster.representative_test_id is not None
        else None
    )

    message = (
        representative.status_message
        if representative and representative.status_message
        else cluster.example_message
    ) or ""
    trace = ""
    if include_trace:
        trace = (
            representative.status_trace
            if representative and representative.status_trace
            else cluster.example_trace_snippet
        ) or ""

    parts = [part for part in (message, trace) if part]
    return "\n".join(parts)


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
    """Вернуть кэшированный экземпляр KB или создать новый (PostgreSQL бэкенд).

    Кэш по ключу (kb_postgres_dsn, min_score, max_results, project_id).
    Предотвращает повторные подключения к БД и re-fit TF-IDF на каждый
    запрос сервера. Feedback store создаётся всегда.
    """
    from alla.knowledge.matcher import MatcherConfig

    global _kb_cache

    cfg = matcher_config if isinstance(matcher_config, MatcherConfig) else None
    cache_key = (
        kb_postgres_dsn,
        cfg.min_score if cfg else 0.15,
        cfg.max_results if cfg else 5,
        project_id,
    )

    if cache_key in _kb_cache:
        logger.debug("KB: используется кэшированный экземпляр")
        return _kb_cache[cache_key]

    from alla.knowledge.postgres_kb import PostgresKnowledgeBase

    from alla.knowledge.postgres_feedback import PostgresFeedbackStore
    feedback_store = PostgresFeedbackStore(dsn=kb_postgres_dsn)

    kb = PostgresKnowledgeBase(
        dsn=kb_postgres_dsn,
        matcher_config=matcher_config,
        project_id=project_id,
        feedback_store=feedback_store,
    )

    _kb_cache[cache_key] = kb
    logger.debug("KB: создан и закэширован новый экземпляр (postgres)")
    return kb
