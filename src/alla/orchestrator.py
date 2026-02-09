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

    Цепочка: триаж → кластеризация → KB-поиск → KB-push → LLM-анализ → LLM-push.

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

    clustering_report = None
    kb_results: dict[str, list[KBMatchResult]] = {}
    kb_push_result = None

    # 2. Кластеризация
    if settings.clustering_enabled and report.failed_tests:
        from alla.services.clustering_service import ClusteringConfig, ClusteringService

        clustering_service = ClusteringService(
            ClusteringConfig(similarity_threshold=settings.clustering_threshold)
        )
        clustering_report = clustering_service.cluster_failures(
            launch_id, report.failed_tests,
        )

    # 3. Поиск по базе знаний
    if settings.kb_enabled and clustering_report is not None:
        from alla.exceptions import KnowledgeBaseError
        from alla.knowledge.matcher import MatcherConfig
        from alla.knowledge.yaml_kb import YamlKnowledgeBase

        try:
            kb = YamlKnowledgeBase(
                kb_path=settings.kb_path,
                matcher_config=MatcherConfig(
                    min_score=settings.kb_min_score,
                    max_results=settings.kb_max_results,
                ),
            )
        except KnowledgeBaseError:
            raise

        for cluster in clustering_report.clusters:
            try:
                matches = kb.search_by_failure(
                    status_message=cluster.example_message,
                    status_trace=cluster.example_trace_snippet,
                    category=cluster.signature.category,
                )
                if matches:
                    kb_results[cluster.cluster_id] = matches
            except Exception as exc:
                logger.warning(
                    "Ошибка KB-поиска для кластера %s: %s",
                    cluster.cluster_id, exc,
                )

    # 4. Запись рекомендаций KB в TestOps
    #    Пропускается если LLM включён — KB-данные интегрируются в LLM-анализ,
    #    и в TestOps пушится единый комментарий от LLM (Stage 6).
    if (
        settings.kb_push_enabled
        and settings.kb_enabled
        and not settings.llm_enabled
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

    # 5. LLM-анализ кластеров через Langflow
    llm_result = None
    llm_push_result = None

    if (
        settings.llm_enabled
        and clustering_report is not None
        and clustering_report.clusters
    ):
        if not settings.langflow_base_url or not settings.langflow_flow_id:
            logger.warning(
                "LLM включён, но не заданы ALLURE_LANGFLOW_BASE_URL "
                "и/или ALLURE_LANGFLOW_FLOW_ID — пропуск"
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
                    )
                except Exception as exc:
                    logger.warning("LLM анализ: ошибка: %s", exc)

    # 6. Запись LLM-рекомендаций в TestOps
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

    return AnalysisResult(
        triage_report=report,
        clustering_report=clustering_report,
        kb_results=kb_results,
        kb_push_result=kb_push_result,
        llm_result=llm_result,
        llm_push_result=llm_push_result,
    )
