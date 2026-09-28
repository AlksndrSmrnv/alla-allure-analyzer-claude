"""Сбор данных прогона: триаж → логи из вложений → кластеризация.

Повторяет начало серверного pipeline (``alla/orchestrator.py``:
``TriageService`` → ``_enrich_with_logs`` → ``_cluster_failures``) на
вендоренном ядре. Только чтение из TestOps: ни комментариев, ни ссылок.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from alla_core.clients.auth import AllureAuthManager
from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_core.models.clustering import ClusteringReport
from alla_core.models.testops import TriageReport
from alla_core.services.clustering_service import ClusteringConfig, ClusteringService
from alla_core.services.log_extraction_service import (
    LogExtractionConfig,
    LogExtractionService,
)
from alla_core.services.triage_service import TriageService

logger = logging.getLogger(__name__)


@dataclass
class LaunchData:
    """Результат сбора: триаж, кластеры и предупреждения для отчёта."""

    triage: TriageReport
    clustering: ClusteringReport | None
    warnings: list[str] = field(default_factory=list)


async def collect_launch(
    launch_id: int,
    settings: Settings,
    progress: Callable[[str], None] | None = None,
) -> LaunchData:
    """Получить результаты прогона из TestOps, обогатить логами и кластеризовать.

    ``progress`` получает короткие строки о стадиях: выгрузка большого прогона
    идёт минуты, и без них не понять, что скрипт жив.
    """
    say = progress or (lambda message: None)
    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )
    warnings: list[str] = []
    async with AllureTestOpsClient(settings, auth) as client:
        say(f"Получаю результаты прогона #{launch_id} из TestOps…")
        triage = await TriageService(client, settings).analyze_launch(launch_id)
        say(
            f"Результатов: {triage.total_results}, активных падений: "
            f"{len(triage.failed_tests)}"
        )
        if triage.failed_tests:
            say("Загружаю логи из вложений упавших тестов…")
            log_service = LogExtractionService(
                client,
                LogExtractionConfig(
                    concurrency=settings.logs_concurrency,
                    max_snippet_chars=settings.logs_max_snippet_chars,
                ),
            )
            try:
                await log_service.enrich_with_logs(triage.failed_tests)
            except Exception as exc:
                logger.warning("Логи из вложений не получены: %s", exc)
                warnings.append(f"Логи из вложений не получены: {exc}")

    if triage.failed_tests:
        say(f"Кластеризую {len(triage.failed_tests)} падений…")
    return LaunchData(
        triage=triage,
        clustering=cluster_failures(launch_id, triage, settings),
        warnings=warnings,
    )


def cluster_failures(
    launch_id: int,
    triage: TriageReport,
    settings: Settings,
) -> ClusteringReport | None:
    """Кластеризовать активные падения с теми же параметрами, что и сервер."""
    if not triage.failed_tests:
        return None
    service = ClusteringService(
        ClusteringConfig(
            similarity_threshold=settings.clustering_threshold,
            log_similarity_weight=settings.logs_clustering_weight,
            step_path_strict_threshold=settings.clustering_step_strict_threshold,
        )
    )
    return service.cluster_failures(launch_id, triage.failed_tests)
