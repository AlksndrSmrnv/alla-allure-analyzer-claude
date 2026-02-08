"""Общая логика анализа запуска — используется и CLI, и HTTP-сервером."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from alla.clients.base import TestResultsProvider, TestResultsUpdater
from alla.config import Settings
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport
from alla.models.testops import TriageReport
from alla.services.kb_push_service import KBPushResult

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Полный результат анализа запуска."""

    triage_report: TriageReport
    clustering_report: ClusteringReport | None = None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_push_result: KBPushResult | None = None


async def analyze_launch(
    launch_id: int,
    client: TestResultsProvider,
    settings: Settings,
    *,
    updater: TestResultsUpdater | None = None,
) -> AnalysisResult:
    """Запустить полный pipeline анализа для одного запуска.

    Цепочка: триаж → кластеризация → KB-поиск → KB-push.

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
    if (
        settings.kb_push_enabled
        and settings.kb_enabled
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
    )
