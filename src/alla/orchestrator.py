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

# Кэш KB: (kb_path, min_score, max_results) → экземпляр.
# Предотвращает повторное чтение YAML и re-fit TF-IDF на каждый запрос сервера.
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
    if settings.logs_enabled and report.failed_tests:
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
            logger.warning(
                "Логи включены (ALLURE_LOGS_ENABLED=true), но провайдер "
                "не реализует AttachmentProvider. Логи пропущены."
            )

    clustering_report = None
    kb_results: dict[str, list[KBMatchResult]] = {}
    kb_push_result = None

    # 2. Кластеризация
    if settings.clustering_enabled and report.failed_tests:
        from alla.services.clustering_service import ClusteringConfig, ClusteringService

        clustering_kwargs: dict = {
            "similarity_threshold": settings.clustering_threshold,
        }
        if settings.logs_enabled and settings.logs_clustering_weight > 0:
            clustering_kwargs["log_similarity_weight"] = settings.logs_clustering_weight

        clustering_service = ClusteringService(
            ClusteringConfig(**clustering_kwargs)
        )
        clustering_report = clustering_service.cluster_failures(
            launch_id, report.failed_tests,
        )

    # 3. Поиск по базе знаний
    if settings.kb_enabled and clustering_report is not None:
        from alla.knowledge.matcher import MatcherConfig

        matcher_config = MatcherConfig(
            min_score=settings.kb_min_score,
            max_results=settings.kb_max_results,
        )
        kb = _get_or_create_kb(
            settings.kb_path,
            matcher_config,
            report.project_id,
            kb_backend=settings.kb_backend,
            kb_postgres_dsn=settings.kb_postgres_dsn,
        )

        # Индекс test_result_id → FailedTestSummary для быстрого lookup
        test_by_id = {t.test_result_id: t for t in report.failed_tests}

        for cluster in clustering_report.clusters:
            try:
                error_text, message_len, trace_len, log_chars, log_test_ids = (
                    _build_kb_query_text(
                        cluster, test_by_id,
                        include_trace=settings.kb_include_trace,
                    )
                )
                if error_text.strip():
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "KB query [%s]: rep_test_id=%s, message_len=%d, "
                            "trace_len=%d, log_chars=%d, log_tests=%s, total_len=%d",
                            cluster.cluster_id,
                            cluster.representative_test_id,
                            message_len,
                            trace_len,
                            log_chars,
                            log_test_ids,
                            len(error_text),
                        )
                        logger.debug(
                            "KB query [%s] preview: head='%s' tail='%s'",
                            cluster.cluster_id,
                            _preview_head(error_text, _KB_QUERY_LOG_PREVIEW_CHARS),
                            _preview_tail(error_text, _KB_QUERY_LOG_PREVIEW_CHARS),
                        )

                    matches = kb.search_by_error(
                        error_text,
                        query_label=cluster.cluster_id,
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
        settings.llm_enabled
        and clustering_report is not None
        and clustering_report.clusters
    ):
        from alla.exceptions import ConfigurationError as _CfgError

        missing: list[str] = []
        if not settings.langflow_base_url:
            missing.append("ALLURE_LANGFLOW_BASE_URL")
        if not settings.langflow_flow_id:
            missing.append("ALLURE_LANGFLOW_FLOW_ID")
        if missing:
            raise _CfgError(
                f"LLM включён (ALLURE_LLM_ENABLED=true), но не заданы: "
                f"{', '.join(missing)}"
            )
        else:
            from alla.clients.langflow_client import LangflowClient
            from alla.services.llm_service import LLMService

            # Суммарная статистика перед LLM-анализом
            _test_by_id_llm = (
                {t.test_result_id: t for t in report.failed_tests}
                if settings.logs_enabled and report.failed_tests
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
                        failed_tests=(
                            report.failed_tests
                            if settings.logs_enabled
                            else None
                        ),
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
        settings.llm_push_enabled
        and settings.llm_enabled
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
        settings.kb_push_enabled
        and settings.kb_enabled
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
    )


def _build_kb_query_text(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    include_trace: bool = True,
) -> tuple[str, int, int, int, list[int]]:
    """Собрать текст запроса для KB из message/trace и ERROR-логов кластера.

    Args:
        cluster: Кластер падений.
        test_by_id: Словарь test_result_id → FailedTestSummary.
        include_trace: Включать ли Allure-trace (stack trace) в query.
            Управляется через ``ALLURE_KB_INCLUDE_TRACE``.
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

    log_sources = _collect_cluster_log_snippets(cluster, test_by_id)
    log_blocks = [
        f"[LOG test_result_id={test_id}]\n{snippet}"
        for test_id, snippet in log_sources
    ]
    log_text = "\n\n".join(log_blocks)

    parts = [part for part in (message, trace, log_text) if part]
    query_text = "\n".join(parts)
    return (
        query_text,
        len(message),
        len(trace),
        len(log_text),
        [test_id for test_id, _ in log_sources],
    )


def _collect_cluster_log_snippets(
    cluster: FailureCluster,
    test_by_id: dict[int, FailedTestSummary],
    *,
    max_logs: int = 3,
) -> list[tuple[int, str]]:
    """Собрать до ``max_logs`` непустых log_snippet из тестов кластера."""
    ordered_test_ids: list[int] = []
    if cluster.representative_test_id is not None:
        ordered_test_ids.append(cluster.representative_test_id)
    ordered_test_ids.extend(cluster.member_test_ids)

    seen: set[int] = set()
    selected: list[tuple[int, str]] = []
    for test_id in ordered_test_ids:
        if test_id in seen:
            continue
        seen.add(test_id)

        summary = test_by_id.get(test_id)
        if summary is None or not summary.log_snippet:
            continue

        snippet = summary.log_snippet.strip()
        if not snippet:
            continue

        selected.append((test_id, snippet))
        if len(selected) >= max_logs:
            break

    return selected


def _preview_head(text: str, max_chars: int) -> str:
    """Сжать head-preview для DEBUG-логов одной строкой."""
    return text[:max_chars].replace("\n", " ")


def _preview_tail(text: str, max_chars: int) -> str:
    """Сжать tail-preview для DEBUG-логов одной строкой."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")


def _get_or_create_kb(
    kb_path: str,
    matcher_config: object,
    project_id: int | None = None,
    *,
    kb_backend: str = "yaml",
    kb_postgres_dsn: str = "",
) -> object:
    """Вернуть кэшированный экземпляр KB или создать новый.

    Поддерживает два бэкенда:
    - 'yaml'     → YamlKnowledgeBase (файловый, по умолчанию)
    - 'postgres' → PostgresKnowledgeBase (загружает из PostgreSQL при init)

    Кэш по ключу (backend, location, min_score, max_results, project_id).
    Предотвращает повторные подключения к БД / чтение YAML на каждый запрос сервера.
    """
    from alla.knowledge.matcher import MatcherConfig

    global _kb_cache

    cfg = matcher_config if isinstance(matcher_config, MatcherConfig) else None
    location = kb_postgres_dsn if kb_backend == "postgres" else kb_path
    cache_key = (
        kb_backend,
        location,
        cfg.min_score if cfg else 0.15,
        cfg.max_results if cfg else 5,
        project_id,
    )

    if cache_key in _kb_cache:
        logger.debug("KB: используется кэшированный экземпляр (backend=%s)", kb_backend)
        return _kb_cache[cache_key]

    if kb_backend == "postgres":
        from alla.knowledge.postgres_kb import PostgresKnowledgeBase

        if not kb_postgres_dsn:
            from alla.exceptions import ConfigurationError
            raise ConfigurationError(
                "kb_backend='postgres' требует ALLURE_KB_POSTGRES_DSN"
            )
        kb = PostgresKnowledgeBase(
            dsn=kb_postgres_dsn,
            matcher_config=matcher_config,
            project_id=project_id,
        )
    else:
        from alla.knowledge.yaml_kb import YamlKnowledgeBase

        kb = YamlKnowledgeBase(
            kb_path=kb_path,
            matcher_config=matcher_config,
            project_id=project_id,
        )

    _kb_cache[cache_key] = kb
    logger.debug("KB: создан и закэширован новый экземпляр (backend=%s)", kb_backend)
    return kb
