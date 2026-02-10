"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from alla.clients.base import TestResultsProvider, TestResultsUpdater
from alla.config import Settings
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport
from alla.models.testops import TriageReport
from alla.services.kb_push_service import KBPushResult

if TYPE_CHECKING:
    from alla.models.llm import LLMAnalysisResult, LLMPushResult

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Полный результат анализа запуска."""

    triage_report: TriageReport
    clustering_report: ClusteringReport | None = None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_push_result: KBPushResult | None = None
    llm_result: LLMAnalysisResult | None = None
    llm_push_result: LLMPushResult | None = None


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
        from alla.exceptions import KnowledgeBaseError
        from alla.knowledge.yaml_kb import YamlKnowledgeBase

        try:
            kb = YamlKnowledgeBase(kb_path=settings.kb_path)
        except KnowledgeBaseError:
            raise

        # Индекс test_result_id → FailedTestSummary для быстрого lookup
        _test_by_id = {t.test_result_id: t for t in report.failed_tests}

        for cluster in clustering_report.clusters:
            try:
                # Собрать полный текст ошибки для KB-поиска:
                # message + полный trace + лог (без обрезки trace до 5 строк)
                rep = _test_by_id.get(cluster.member_test_ids[0]) if cluster.member_test_ids else None
                error_parts: list[str] = []
                if rep:
                    if rep.status_message:
                        error_parts.append(rep.status_message)
                    if rep.status_trace:
                        error_parts.append(rep.status_trace)
                    if rep.log_snippet:
                        error_parts.append(rep.log_snippet)
                else:
                    if cluster.example_message:
                        error_parts.append(cluster.example_message)
                    if cluster.example_trace_snippet:
                        error_parts.append(cluster.example_trace_snippet)

                error_text = "\n".join(error_parts)
                if error_text.strip():
                    matches = kb.search_by_error(error_text)
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

            async with LangflowClient(
                base_url=settings.langflow_base_url,
                flow_id=settings.langflow_flow_id,
                api_key=settings.langflow_api_key,
                timeout=settings.llm_timeout,
                ssl_verify=settings.ssl_verify,
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
    )
