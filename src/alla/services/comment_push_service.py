"""Публичный сервис форматирования и постинга комментариев в Allure TestOps.

Один контракт, через который комментарии пишут и server-side LLM-путь
(GigaChat-результат), и skill-режим (агентский результат, обёрнутый в
:class:`LLMAnalysisResult` через
:mod:`alla.services.agent_analysis_adapter`).

Раньше эти функции жили в :mod:`alla.services.llm_service`; теперь они
переехали сюда, а старые имена остаются как re-export для бэквард-
совместимости.
"""

from __future__ import annotations

import asyncio
import logging

from alla.clients.base import TestResultsUpdater
from alla.models.clustering import ClusteringReport
from alla.models.llm import LLMAnalysisResult, LLMPushResult
from alla.models.testops import TriageReport

logger = logging.getLogger(__name__)

__all__ = [
    "format_comment",
    "push_comments",
    "ALLA_COMMENT_HEADER",
    "ALLA_COMMENT_SEPARATOR",
]

ALLA_COMMENT_HEADER = "[alla] LLM-анализ ошибки"
ALLA_COMMENT_SEPARATOR = "=" * 40


def format_comment(
    analysis_text: str,
    *,
    step_path: str | None = None,
) -> str:
    """Отформатировать текст анализа в комментарий ``[alla]`` для TestOps.

    Префикс ``[alla]`` нужен, чтобы :class:`alla.services.comment_delete_service.CommentDeleteService`
    позже мог уверенно удалить такие комментарии.
    """
    parts = [ALLA_COMMENT_HEADER, ALLA_COMMENT_SEPARATOR, ""]
    if step_path:
        parts.append(f"Шаг теста: {step_path}")
        parts.append("")
    parts.append(analysis_text)
    return "\n".join(parts)


async def push_comments(
    clustering_report: ClusteringReport,
    analysis_result: LLMAnalysisResult,
    triage_report: TriageReport,
    updater: TestResultsUpdater,
    *,
    concurrency: int = 10,
) -> LLMPushResult:
    """Записать ``analysis_result.cluster_analyses`` в TestOps как комментарии.

    Поведение совпадает с прежней ``push_llm_results``: дедупликация по
    ``test_case_id``, semaphore + gather, per-test resilience.

    Имя ``analysis_result`` намеренно общее: с точки зрения этого сервиса
    источник анализа (LLM или агент CLI) не важен.
    """
    test_case_ids: dict[int, int | None] = {
        t.test_result_id: t.test_case_id for t in triage_report.failed_tests
    }

    comments: dict[int, str] = {}
    skipped = 0

    for cluster in clustering_report.clusters:
        analysis = analysis_result.cluster_analyses.get(cluster.cluster_id)
        if not analysis or not analysis.analysis_text:
            skipped += len(cluster.member_test_ids)
            continue

        comment_text = format_comment(
            analysis.analysis_text,
            step_path=cluster.example_step_path,
        )

        for test_id in cluster.member_test_ids:
            tc_id = test_case_ids.get(test_id)
            if tc_id is None:
                logger.warning(
                    "Comment push: test_result %d не имеет test_case_id, пропуск",
                    test_id,
                )
                skipped += 1
                continue

            if tc_id in comments:
                skipped += 1
                continue

            comments[tc_id] = comment_text

    if not comments:
        logger.info(
            "Comment push: нет комментариев для записи "
            "(0 кластеров с анализом или нет test_case_id)"
        )
        return LLMPushResult(
            total_tests=clustering_report.total_failures,
            updated_count=0,
            failed_count=0,
            skipped_count=skipped,
        )

    logger.info(
        "Comment push: отправка комментариев для %d тест-кейсов (параллелизм=%d)",
        len(comments),
        concurrency,
    )

    semaphore = asyncio.Semaphore(concurrency)
    updated = 0
    failed_push = 0

    async def post_one(tc_id: int, text: str) -> bool:
        async with semaphore:
            try:
                await updater.post_comment(tc_id, text)
                return True
            except Exception as exc:
                logger.warning(
                    "Comment push: не удалось добавить комментарий "
                    "для тест-кейса %d: %s",
                    tc_id,
                    exc,
                )
                return False

    tasks = [post_one(tc_id, text) for tc_id, text in comments.items()]
    results = await asyncio.gather(*tasks)

    for success in results:
        if success:
            updated += 1
        else:
            failed_push += 1

    logger.info(
        "Comment push: завершено. Комментариев: %d, ошибок: %d, пропущено: %d",
        updated,
        failed_push,
        skipped,
    )

    return LLMPushResult(
        total_tests=clustering_report.total_failures,
        updated_count=updated,
        failed_count=failed_push,
        skipped_count=skipped,
    )
