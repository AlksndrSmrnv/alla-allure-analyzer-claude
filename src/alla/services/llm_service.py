"""Сервис LLM-анализа кластеров ошибок через Langflow."""

from __future__ import annotations

import asyncio
import logging

from alla.clients.base import TestResultsUpdater
from alla.clients.langflow_client import LangflowClient
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, LLMPushResult
from alla.models.testops import FailedTestSummary, TriageReport

logger = logging.getLogger(__name__)

_LLM_HEADER = "[alla] LLM-анализ ошибки"
_SEPARATOR = "=" * 40


def build_cluster_prompt(
    cluster: FailureCluster,
    kb_matches: list[KBMatchResult] | None = None,
    log_snippet: str | None = None,
) -> str:
    """Собрать промпт для LLM-анализа одного кластера.

    Включает: label, member_count, example_message, example_trace_snippet,
    опционально log_snippet и KB-совпадения для контекста.
    """
    parts: list[str] = [
        "Ты — инженер по анализу сбоев автотестов. Твоя задача — провести "
        "детальный технический разбор ошибки и дать конкретный, действенный "
        "ответ. Запрещены общие фразы вида «проверьте сервер», "
        "«проверьте тестовые данные», «обратитесь к команде разработки». "
        "Вместо этого — называй конкретные классы, методы, строки, параметры, "
        "HTTP-коды, SQL-запросы, имена сервисов, которые фигурируют в ошибке.",
        "",
        "═══════════════════════════════════════",
        "ДАННЫЕ ДЛЯ АНАЛИЗА",
        "═══════════════════════════════════════",
        "",
        f"Кластер: {cluster.label}",
        f"Количество тестов с этой ошибкой: {cluster.member_count}",
    ]

    if cluster.example_message:
        msg = cluster.example_message
        if len(msg) > 2000:
            msg = msg[:2000] + "...[обрезано]"
        parts.append("")
        parts.append("--- Сообщение об ошибке ---")
        parts.append(msg)

    if cluster.example_trace_snippet:
        trace = cluster.example_trace_snippet
        if len(trace) > 3000:
            trace = trace[:3000] + "...[обрезано]"
        parts.append("")
        parts.append("--- Стек-трейс ---")
        parts.append(trace)

    if log_snippet:
        snippet = log_snippet
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "...[обрезано]"
        parts.append("")
        parts.append("--- Фрагмент лога приложения ---")
        parts.append(snippet)

    if kb_matches:
        parts.append("")
        parts.append("--- Совпадения из базы знаний (справочно) ---")
        for m in kb_matches[:3]:
            parts.append(f"  [{m.score:.2f}] {m.entry.title}")
            parts.append(f"    Описание: {m.entry.description}")
            parts.append(f"    Причина: {m.entry.root_cause.value}")
            parts.append(f"    Срочность: {m.entry.severity.value}")
            if m.entry.resolution_steps:
                parts.append("    Шаги по устранению:")
                for step in m.entry.resolution_steps:
                    parts.append(f"      - {step}")
            if m.entry.related_links:
                parts.append(f"    Ссылки: {', '.join(m.entry.related_links)}")

    parts.append("")
    parts.append("═══════════════════════════════════════")
    parts.append("ИНСТРУКЦИИ ПО АНАЛИЗУ")
    parts.append("═══════════════════════════════════════")
    parts.append("")
    parts.append(
        "Внимательно прочитай сообщение об ошибке, стек-трейс и лог. "
        "Проведи анализ по следующему плану. Каждый пункт обязателен.\n"
        "\n"
        "1. ЧТО ИМЕННО ПРОИЗОШЛО\n"
        "   Разбери ошибку технически. Назови:\n"
        "   - Тип исключения/ошибки (например: NullPointerException, "
        "AssertionError, HTTP 502, timeout)\n"
        "   - Класс и метод, в котором произошёл сбой (из стек-трейса)\n"
        "   - Конкретное значение или условие, которое привело к ошибке "
        "(например: «метод getUser() вернул null для userId=...», "
        "«ответ сервера: 502 Bad Gateway на эндпоинт /api/v2/checkout», "
        "«таймаут 30с при подключении к redis-cluster-03:6379»)\n"
        "   - Если в стек-трейсе видны номера строк — укажи их "
        "(например: «UserService.java:142»)\n"
        "\n"
        "2. ВЕРОЯТНАЯ КОРНЕВАЯ ПРИЧИНА\n"
        "   Определи одну из категорий: тест / приложение / окружение / данные.\n"
        "   Объясни, ПОЧЕМУ ты выбрал именно эту категорию, ссылаясь на "
        "конкретные фрагменты ошибки. Например:\n"
        "   - «Причина — приложение: метод PaymentService.charge() "
        "не обрабатывает null-значение поля cardToken, "
        "что видно из NPE в PaymentService.java:87»\n"
        "   - «Причина — окружение: тест не смог подключиться к "
        "postgres-replica-02.internal:5432, "
        "ConnectionRefused указывает на недоступность реплики»\n"
        "   - «Причина — тест: assert проверяет точное значение "
        "timestamp, которое меняется при каждом запуске»\n"
        "\n"
        "3. ЧТО КОНКРЕТНО ДЕЛАТЬ\n"
        "   Дай 1–3 конкретных шага для исправления. Каждый шаг должен "
        "содержать: что именно изменить/проверить/запустить, в каком файле "
        "или сервисе, какой результат ожидается. Примеры хороших шагов:\n"
        "   - «Добавить null-check в PaymentService.charge() "
        "перед обращением к cardToken (строка ~87)»\n"
        "   - «Проверить доступность postgres-replica-02 командой: "
        "pg_isready -h postgres-replica-02.internal -p 5432»\n"
        "   - «Заменить точное сравнение timestamp на проверку диапазона "
        "±5 секунд в тесте test_order_creation»\n"
        "   Запрещены общие рекомендации без привязки к конкретным "
        "артефактам из ошибки.\n"
        "\n"
        "4. КРИТИЧНОСТЬ: critical / high / medium / low\n"
        "   Кратко обоснуй (одно предложение).\n"
        "\n"
        "Если данных недостаточно для точного вывода — скажи, каких данных "
        "не хватает и где их взять (конкретное имя лога, endpoint, таблица), "
        "вместо того чтобы угадывать."
    )

    return "\n".join(parts)


def format_llm_comment(analysis_text: str) -> str:
    """Отформатировать LLM-анализ в текст комментария для TestOps."""
    return "\n".join([_LLM_HEADER, _SEPARATOR, "", analysis_text])


class LLMService:
    """Анализ кластеров ошибок через Langflow LLM.

    Для каждого кластера: строит промпт, вызывает Langflow, сохраняет результат.
    """

    def __init__(
        self,
        langflow_client: LangflowClient,
        *,
        concurrency: int = 3,
    ) -> None:
        self._client = langflow_client
        self._concurrency = concurrency

    async def analyze_clusters(
        self,
        clustering_report: ClusteringReport,
        kb_results: dict[str, list[KBMatchResult]] | None = None,
        failed_tests: list[FailedTestSummary] | None = None,
    ) -> LLMAnalysisResult:
        """Проанализировать все кластеры через LLM.

        Args:
            clustering_report: Отчёт кластеризации.
            kb_results: Опционально — KB-совпадения для обогащения промпта.
            failed_tests: Опционально — список тестов для извлечения log_snippet.

        Returns:
            LLMAnalysisResult со всеми анализами.
        """
        if not clustering_report.clusters:
            return LLMAnalysisResult(
                total_clusters=0,
                analyzed_count=0,
                failed_count=0,
                skipped_count=0,
            )

        # Индекс test_result_id → FailedTestSummary для быстрого lookup
        test_by_id: dict[int, FailedTestSummary] = {}
        if failed_tests:
            test_by_id = {t.test_result_id: t for t in failed_tests}

        semaphore = asyncio.Semaphore(self._concurrency)
        analyses: dict[str, LLMClusterAnalysis] = {}
        analyzed = 0
        failed = 0
        skipped = 0

        async def analyze_one(cluster: FailureCluster) -> None:
            nonlocal analyzed, failed, skipped

            if not cluster.example_message and not cluster.example_trace_snippet:
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

            kb_matches = (kb_results or {}).get(cluster.cluster_id)

            # Получить log_snippet представителя кластера
            log_snippet: str | None = None
            if test_by_id and cluster.member_test_ids:
                rep = test_by_id.get(cluster.member_test_ids[0])
                if rep:
                    log_snippet = rep.log_snippet

            prompt = build_cluster_prompt(cluster, kb_matches, log_snippet)

            async with semaphore:
                try:
                    result_text = await self._client.run_flow(prompt)
                    analyses[cluster.cluster_id] = LLMClusterAnalysis(
                        cluster_id=cluster.cluster_id,
                        analysis_text=result_text,
                    )
                    analyzed += 1
                    logger.debug(
                        "LLM: кластер %s проанализирован (%d символов)",
                        cluster.cluster_id,
                        len(result_text),
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
        )


async def push_llm_results(
    clustering_report: ClusteringReport,
    llm_result: LLMAnalysisResult,
    triage_report: TriageReport,
    updater: TestResultsUpdater,
    *,
    concurrency: int = 10,
) -> LLMPushResult:
    """Записать LLM-рекомендации в TestOps через комментарии.

    Паттерн повторяет KBPushService.push_kb_results():
    дедупликация по test_case_id, semaphore+gather, per-test error resilience.

    Args:
        clustering_report: Отчёт кластеризации.
        llm_result: Результаты LLM-анализа.
        triage_report: Отчёт триажа (для получения test_case_id).
        updater: Провайдер для записи комментариев.
        concurrency: Макс. параллельных запросов.

    Returns:
        LLMPushResult со статистикой обновлений.
    """
    test_case_ids: dict[int, int | None] = {
        t.test_result_id: t.test_case_id for t in triage_report.failed_tests
    }

    comments: dict[int, str] = {}
    skipped = 0

    for cluster in clustering_report.clusters:
        analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
        if not analysis or not analysis.analysis_text:
            skipped += len(cluster.member_test_ids)
            continue

        comment_text = format_llm_comment(analysis.analysis_text)

        for test_id in cluster.member_test_ids:
            tc_id = test_case_ids.get(test_id)
            if tc_id is None:
                logger.warning(
                    "LLM push: test_result %d не имеет test_case_id, пропуск",
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
            "LLM push: нет комментариев для записи "
            "(0 кластеров с LLM-анализом или нет test_case_id)"
        )
        return LLMPushResult(
            total_tests=clustering_report.total_failures,
            updated_count=0,
            failed_count=0,
            skipped_count=skipped,
        )

    logger.info(
        "LLM push: отправка комментариев для %d тест-кейсов "
        "(параллелизм=%d)",
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
                    "LLM push: не удалось добавить комментарий "
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
        "LLM push: завершено. Комментариев: %d, ошибок: %d, пропущено: %d",
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
