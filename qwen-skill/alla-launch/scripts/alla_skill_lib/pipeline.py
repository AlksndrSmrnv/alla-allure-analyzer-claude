"""Сбор данных прогона: триаж → логи из вложений → кластеризация.

Использует ядро ``alla_core``: ``TriageService`` → извлечение логов
→ ``ClusteringService``. Только чтение из TestOps: ни комментариев, ни ссылок.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
from alla_core.services.retry_linking import retry_warnings
from alla_core.services.triage_service import TriageService, fetch_warnings

from alla_skill_lib.images import StagedImage, fetch_images, plan_images

logger = logging.getLogger(__name__)


@dataclass
class LaunchData:
    """Результат сбора: триаж, кластеры и предупреждения для отчёта."""

    triage: TriageReport
    clustering: ClusteringReport | None
    warnings: list[str] = field(default_factory=list)
    # Скриншоты тестов-примеров во временной папке: id результата → файл.
    images: dict[int, StagedImage] = field(default_factory=dict)


async def collect_launch(
    launch_id: int,
    settings: Settings,
    progress: Callable[[str], None] | None = None,
    images_dir: Path | None = None,
) -> LaunchData:
    """Получить результаты прогона из TestOps, обогатить логами и кластеризовать.

    ``progress`` получает короткие строки о стадиях: выгрузка большого прогона
    идёт минуты, и без них не понять, что скрипт жив. ``images_dir`` — временная папка
    для скриншотов тестов-примеров (они скачиваются после кластеризации: нужны только
    тестам, чьи данные увидит модель); ``None`` — скриншоты не скачиваются.
    """
    say = progress or (lambda message: None)
    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
        retries=settings.http_retries,
    )
    warnings: list[str] = []
    async with AllureTestOpsClient(settings, auth) as client:
        say(f"Получаю результаты прогона #{launch_id} из TestOps…")
        triage = await TriageService(client, settings).analyze_launch(launch_id)
        warnings.extend(fetch_warnings(triage.fetch, launch_id))
        warnings.extend(retry_warnings(triage.retries))
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
                    operation_id_keys=settings.operation_keys,
                ),
            )
            try:
                await log_service.enrich_with_logs(triage.failed_tests)
            except Exception as exc:
                logger.warning("Логи из вложений не получены: %s", exc)
                warnings.append(f"Логи из вложений не получены: {exc}")

        if triage.failed_tests:
            say(f"Кластеризую {len(triage.failed_tests)} падений…")
        clustering = cluster_failures(launch_id, triage, settings)
        images: dict[int, StagedImage] = {}
        plan = plan_images(clustering.clusters, {
            test.test_result_id: test for test in triage.failed_tests
        }, settings.images_per_problem) if clustering is not None and images_dir else []
        if plan and images_dir is not None:
            say(f"Скачиваю скриншоты тестов-примеров: {len(plan)}…")
            images = await fetch_images(client, plan, images_dir,
                                        max_bytes=settings.images_max_bytes,
                                        concurrency=settings.logs_concurrency)
    return LaunchData(triage=triage, clustering=clustering, warnings=warnings, images=images)


def cluster_failures(
    launch_id: int,
    triage: TriageReport,
    settings: Settings,
) -> ClusteringReport | None:
    """Кластеризовать активные падения с параметрами кластеризации из настроек."""
    if not triage.failed_tests:
        return None
    service = ClusteringService(
        ClusteringConfig(
            similarity_threshold=settings.clustering_threshold,
            log_similarity_weight=settings.logs_clustering_weight,
            step_path_strict_threshold=settings.clustering_step_strict_threshold,
            log_split_threshold=settings.clustering_log_split_threshold,
            resource_gate=settings.clustering_resource_gate,
        )
    )
    return service.cluster_failures(launch_id, triage.failed_tests)
