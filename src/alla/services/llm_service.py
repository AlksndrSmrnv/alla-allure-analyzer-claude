"""Сервис LLM-анализа кластеров ошибок через GigaChat.

После рефакторинга prompt builder и comment push живут в публичных
сервисах:

* :mod:`alla.services.prompt_builder_service` — единый prompt builder
  (используется этим сервисом и skill-режимом).
* :mod:`alla.services.comment_push_service` — единая функция postинга
  комментариев в TestOps.

Старые имена ``build_cluster_prompt`` / ``build_launch_summary_prompt`` /
``format_llm_comment`` / ``push_llm_results`` оставлены здесь как тонкие
обёртки/re-export для обратной совместимости.
"""

import asyncio
import logging
from typing import Protocol

from alla.clients.gigachat_client import ChatResponse
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.llm import (
    LLMAnalysisResult,
    LLMClusterAnalysis,
    LLMLaunchSummary,
    TokenUsage,
)
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.comment_push_service import (
    format_comment as format_llm_comment,
    push_comments as push_llm_results,
)
from alla.services.prompt_builder_service import (
    DEFAULT_LOG_MAX_CHARS as _PROMPT_LOG_MAX_CHARS,
    DEFAULT_MESSAGE_MAX_CHARS as _PROMPT_MESSAGE_MAX_CHARS,
    DEFAULT_TRACE_MAX_CHARS as _PROMPT_TRACE_MAX_CHARS,
    build_cluster_analysis_prompt,
    build_launch_summary_prompt as _build_launch_summary_prompt_impl,
)
from alla.utils.log_utils import has_explicit_errors

logger = logging.getLogger(__name__)


__all__ = [
    "LLMClient",
    "LLMService",
    "build_cluster_prompt",
    "build_launch_summary_prompt",
    "format_llm_comment",
    "push_llm_results",
]


class LLMClient(Protocol):
    """Протокол LLM-клиента для LLMService."""

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse: ...


def build_cluster_prompt(
    cluster: FailureCluster,
    kb_matches: list[KBMatchResult] | None = None,
    log_snippet: str | None = None,
    full_trace: str | None = None,
    *,
    kb_query_provenance: tuple[int, int, int] | None = None,
    message_max_chars: int = _PROMPT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = _PROMPT_TRACE_MAX_CHARS,
    log_max_chars: int = _PROMPT_LOG_MAX_CHARS,
) -> tuple[str, str]:
    """Старый интерфейс ``(system_prompt, user_prompt)`` для GigaChat-пути.

    Делегирует в :func:`alla.services.prompt_builder_service.build_cluster_analysis_prompt`.
    """
    prompt = build_cluster_analysis_prompt(
        cluster,
        kb_matches=kb_matches,
        log_snippet=log_snippet,
        full_trace=full_trace,
        kb_query_provenance=kb_query_provenance,
        message_max_chars=message_max_chars,
        trace_max_chars=trace_max_chars,
        log_max_chars=log_max_chars,
    )
    return prompt.system_prompt, prompt.user_prompt


def build_launch_summary_prompt(
    clustering_report: ClusteringReport,
    triage_report: TriageReport,
    llm_result: LLMAnalysisResult | None = None,
) -> tuple[str, str]:
    """Старый интерфейс ``(system_prompt, user_prompt)`` для GigaChat-пути.

    Делегирует в :func:`alla.services.prompt_builder_service.build_launch_summary_prompt`.
    """
    prompt = _build_launch_summary_prompt_impl(
        clustering_report,
        triage_report,
        llm_result,
    )
    return prompt.system_prompt, prompt.user_prompt


class LLMService:
    """Анализ кластеров ошибок через LLM.

    Для каждого кластера: строит промпт через
    :mod:`alla.services.prompt_builder_service`, вызывает LLM,
    сохраняет результат. Между запросами выдерживается минимальный
    интервал ``request_delay`` для предотвращения 429 ошибок от
    GigaChat API.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        concurrency: int = 3,
        request_delay: float = 0.5,
        message_max_chars: int = _PROMPT_MESSAGE_MAX_CHARS,
        trace_max_chars: int = _PROMPT_TRACE_MAX_CHARS,
        log_max_chars: int = _PROMPT_LOG_MAX_CHARS,
    ) -> None:
        self._client = client
        self._concurrency = concurrency
        self._request_delay = request_delay
        self._message_max_chars = message_max_chars
        self._trace_max_chars = trace_max_chars
        self._log_max_chars = log_max_chars
        self._rate_lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def _rate_limited_chat(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> ChatResponse:
        """Вызвать LLM с минимальным интервалом между запросами."""
        if self._request_delay > 0:
            async with self._rate_lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                elapsed = now - self._last_request_time
                if (
                    self._last_request_time > 0
                    and elapsed < self._request_delay
                ):
                    await asyncio.sleep(self._request_delay - elapsed)
                self._last_request_time = loop.time()

        return await self._client.chat(system_prompt, user_prompt)

    async def analyze_clusters(
        self,
        clustering_report: ClusteringReport,
        kb_results: dict[str, list[KBMatchResult]] | None = None,
        failed_tests: list[FailedTestSummary] | None = None,
        kb_provenance: dict[str, tuple[int, int, int]] | None = None,
    ) -> LLMAnalysisResult:
        """Проанализировать все кластеры через LLM."""
        if not clustering_report.clusters:
            return LLMAnalysisResult(
                total_clusters=0,
                analyzed_count=0,
                failed_count=0,
                skipped_count=0,
            )

        test_by_id: dict[int, FailedTestSummary] = {}
        if failed_tests:
            test_by_id = {t.test_result_id: t for t in failed_tests}

        semaphore = asyncio.Semaphore(self._concurrency)
        analyses: dict[str, LLMClusterAnalysis] = {}
        analyzed = 0
        failed = 0
        skipped = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0

        async def analyze_one(cluster: FailureCluster) -> None:
            nonlocal analyzed, failed, skipped
            nonlocal total_prompt_tokens, total_completion_tokens, total_tokens

            kb_matches = (kb_results or {}).get(cluster.cluster_id)

            # Получить log_snippet и full_trace представителя (fallback на members кластера)
            log_snippet: str | None = None
            full_trace: str | None = None
            if test_by_id and cluster.representative_test_id is not None:
                rep = test_by_id.get(cluster.representative_test_id)
                if rep:
                    if rep.log_snippet and rep.log_snippet.strip():
                        log_snippet = rep.log_snippet
                    full_trace = rep.status_trace
            if not log_snippet and test_by_id:
                for tid in cluster.member_test_ids:
                    member = test_by_id.get(tid)
                    if member and member.log_snippet and member.log_snippet.strip():
                        log_snippet = member.log_snippet
                        break

            has_any_text = (
                cluster.example_message
                or cluster.example_trace_snippet
                or (log_snippet and log_snippet.strip())
            )
            if not has_any_text:
                logger.debug(
                    "LLM: кластер %s пропущен (нет текста ошибки)",
                    cluster.cluster_id,
                )
                skipped += 1
                analyses[cluster.cluster_id] = LLMClusterAnalysis(
                    cluster_id=cluster.cluster_id,
                    error="Нет текста ошибки для анализа",
                )
                return

            has_log = bool(log_snippet and log_snippet.strip())
            has_log_errors = (
                has_explicit_errors(log_snippet) if has_log else False
            )
            kb_count = len(kb_matches) if kb_matches else 0

            logger.info(
                "LLM: кластер %s (%d тестов) — "
                "лог отправлен: %s, ошибки в логе: %s, "
                "совпадений с базой знаний: %d",
                cluster.cluster_id[:8],
                cluster.member_count,
                "да" if has_log else "нет",
                "да" if has_log_errors else "нет",
                kb_count,
            )

            provenance = (kb_provenance or {}).get(cluster.cluster_id)
            prompt = build_cluster_analysis_prompt(
                cluster,
                kb_matches=kb_matches,
                log_snippet=log_snippet,
                full_trace=full_trace,
                kb_query_provenance=provenance,
                message_max_chars=self._message_max_chars,
                trace_max_chars=self._trace_max_chars,
                log_max_chars=self._log_max_chars,
            )

            async with semaphore:
                try:
                    chat_response = await self._rate_limited_chat(
                        prompt.system_prompt, prompt.user_prompt,
                    )
                    analyses[cluster.cluster_id] = LLMClusterAnalysis(
                        cluster_id=cluster.cluster_id,
                        analysis_text=chat_response.text,
                    )
                    analyzed += 1
                    usage = chat_response.token_usage
                    total_prompt_tokens += usage.prompt_tokens
                    total_completion_tokens += usage.completion_tokens
                    total_tokens += usage.total_tokens
                    logger.debug(
                        "LLM: кластер %s проанализирован (%d символов, %d токенов)",
                        cluster.cluster_id,
                        len(chat_response.text),
                        usage.total_tokens,
                    )
                except Exception as exc:
                    logger.warning(
                        "LLM: ошибка анализа кластера %s: %s",
                        cluster.cluster_id,
                        exc,
                    )
                    failed += 1
                    analyses[cluster.cluster_id] = LLMClusterAnalysis(
                        cluster_id=cluster.cluster_id,
                        error=str(exc),
                    )

        tasks = [analyze_one(c) for c in clustering_report.clusters]
        await asyncio.gather(*tasks)

        logger.info(
            "LLM: анализ завершён. Успешно: %d, ошибок: %d, пропущено: %d",
            analyzed,
            failed,
            skipped,
        )

        return LLMAnalysisResult(
            total_clusters=len(clustering_report.clusters),
            analyzed_count=analyzed,
            failed_count=failed,
            skipped_count=skipped,
            cluster_analyses=analyses,
            token_usage=TokenUsage(
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                total_tokens=total_tokens,
            ),
        )

    async def generate_launch_summary(
        self,
        clustering_report: ClusteringReport,
        triage_report: TriageReport,
        llm_result: LLMAnalysisResult | None = None,
    ) -> LLMLaunchSummary:
        """Сформировать итоговый отчёт по прогону через LLM."""
        prompt = _build_launch_summary_prompt_impl(
            clustering_report, triage_report, llm_result,
        )
        logger.info(
            "LLM summary: запрос итогового отчёта по %d кластерам",
            clustering_report.cluster_count,
        )
        try:
            chat_response = await self._rate_limited_chat(
                prompt.system_prompt, prompt.user_prompt,
            )
            logger.info(
                "LLM summary: отчёт получен (%d символов)",
                len(chat_response.text),
            )
            return LLMLaunchSummary(
                summary_text=chat_response.text,
                token_usage=chat_response.token_usage,
            )
        except Exception as exc:
            logger.warning("LLM summary: ошибка: %s", exc)
            return LLMLaunchSummary(summary_text="", error=str(exc))
