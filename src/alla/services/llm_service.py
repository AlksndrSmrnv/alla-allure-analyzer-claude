"""Сервис LLM-анализа кластеров ошибок через Langflow."""

from __future__ import annotations

import asyncio
import logging
from alla.clients.base import TestResultsUpdater
from alla.clients.langflow_client import LangflowClient
from alla.knowledge.models import KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, LLMLaunchSummary, LLMPushResult
from alla.models.testops import FailedTestSummary, TriageReport
from alla.utils.log_utils import has_explicit_errors
from alla.utils.text_normalization import normalize_text_for_llm

logger = logging.getLogger(__name__)

_LLM_HEADER = "[alla] LLM-анализ ошибки"
_SEPARATOR = "=" * 40
_EXACT_KB_SCORE = 0.999  # Нужен для метки EXACT MATCH в промпте.
_PROMPT_MESSAGE_MAX_CHARS = 2000
_PROMPT_TRACE_MAX_CHARS = 400
_PROMPT_LOG_MAX_CHARS = 8000
_TRUNCATION_SUFFIX = "...[обрезано]"

def _interpret_kb_score(score: float) -> str:
    """Перевести score совпадения с базой знаний в текстовое описание уверенности."""
    if score >= 0.7:
        return "высокое совпадение"
    if score >= 0.4:
        return "частичное совпадение"
    return "слабое совпадение"


def _format_kb_category(category: RootCauseCategory) -> str:
    """Перевести категорию записи из базы знаний в формулировку для LLM."""
    mapping = {
        RootCauseCategory.TEST: "тест",
        RootCauseCategory.SERVICE: "приложение",
        RootCauseCategory.ENV: "окружение",
        RootCauseCategory.DATA: "данные",
    }
    return mapping.get(category, str(category))


def _is_exact_kb_match(match: KBMatchResult) -> bool:
    """Определить, что совпадение с базой знаний является точным."""
    if match.match_origin == "feedback_exact":
        return True
    if match.score < _EXACT_KB_SCORE:
        return False
    if not match.matched_on:
        return True
    return any(
        "Tier 1" in reason or "exact substring" in reason.lower()
        for reason in match.matched_on
    )


def _humanize_match_reason(matched_on: list[str]) -> str:
    """Перевести технические tier-описания в понятный для LLM текст."""
    if not matched_on:
        return "текстовое совпадение"
    reason = matched_on[0]
    if "Feedback memory" in reason:
        return (
            "Пользователь ранее подтвердил эту запись для точной сигнатуры "
            "той же ошибки"
        )
    if "Tier 1" in reason or "exact substring" in reason.lower():
        return "Пример ошибки из базы знаний найден целиком в данных кластера"
    if "Tier 2" in reason or "line match" in reason.lower():
        return (
            "Большинство строк примера ошибки из базы знаний найдены "
            "в данных кластера (построчное сопоставление)"
        )
    if "Tier 3" in reason or "TF-IDF" in reason:
        return "Нечёткое текстовое совпадение (похожие слова и фразы)"
    return reason


def _truncate_prompt_text(text: str, max_chars: int) -> str:
    """Обрезать текст для prompt по началу, сохранив явную пометку."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_SUFFIX


def build_cluster_prompt(
    cluster: FailureCluster,
    kb_matches: list[KBMatchResult] | None = None,
    log_snippet: str | None = None,
    full_trace: str | None = None,
    *,
    kb_query_provenance: tuple[int, int, int] | None = None,
) -> str:
    """Собрать промпт для LLM-анализа одного кластера.

    Включает: label, member_count, example_message, full_trace (или snippet),
    опционально log_snippet и совпадения с базой знаний для контекста.

    Args:
        kb_query_provenance: (message_len, trace_len, log_len) — длины сегментов
            запроса к базе знаний для указания LLM, по каким данным было найдено совпадение.
    """
    parts: list[str] = [
        "Ты — инженер по анализу сбоев автотестов.",
        "Твоя задача: быстро и точно определить причину падения и дать полезные шаги исправления.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши ТОЛЬКО то, что видишь в данных ниже. "
        "Не додумывай, не предполагай, не фантазируй. "
        "Если чего-то нет в ошибке, стек-трейсе, логе или базе знаний — не упоминай это.",
        "",
        "ПРИОРИТЕТ ИСТОЧНИКОВ:",
        "1. БАЗА ЗНАНИЙ — основной источник интерпретации причины и способа исправления, если есть совпадение.",
        "2. Сообщение ошибки / стек / лог — нужны, чтобы подтвердить или опровергнуть совпадение из базы знаний.",
        "3. Не придумывай альтернативную причину, если запись базы знаний #1 хорошо подходит и данные ей не противоречат.",
        "",
        "═══════════════════════════════════════",
        "ДАННЫЕ",
        "═══════════════════════════════════════",
        "",
        f"Кластер: {cluster.label}",
        f"Затронуто тестов: {cluster.member_count}",
    ]

    if cluster.example_step_path:
        parts.append(f"Шаг теста: {cluster.example_step_path}")

    if kb_matches:
        parts.append("\n--- База знаний (читай первой) ---")
        if _is_exact_kb_match(kb_matches[0]):
            parts.append(
                "ТОЧНОЕ СОВПАДЕНИЕ: запись базы знаний #1 имеет score 1.00. "
                "Это обязательная основная причина; записи базы знаний #2 и #3 игнорируй, "
                "если нет прямого противоречия в данных."
            )
        parts.append(
            "Правило: если запись базы знаний #1 имеет score >= 0.70 и не противоречит ошибке, "
            "используй её как основную причину."
        )
        parts.append(
            "Если score 0.40-0.69 — используй запись базы знаний #1 как основную гипотезу "
            "и обязательно подтвердись строкой из ошибки, трейса или лога."
        )
        for index, m in enumerate(kb_matches[:3], start=1):
            entry = m.entry
            confidence = _interpret_kb_score(m.score)
            if index == 1 and _is_exact_kb_match(m):
                role = "exact match; обязательная основная причина"
            elif index == 1:
                role = "основная гипотеза"
            else:
                role = "дополнительное совпадение"
            parts.append(
                f"\nСовпадение из базы знаний #{index} [{role}; {confidence}; score {m.score:.2f}]"
            )
            parts.append(f"Название: {entry.title}")
            parts.append(f"Категория: {_format_kb_category(entry.category)}")
            parts.append(f"Почему похоже: {_humanize_match_reason(m.matched_on)}")
            if index <= 2 and entry.error_example:
                example_text = entry.error_example
                if len(example_text) > 500:
                    example_text = example_text[:500] + "...[обрезано]"
                parts.append(
                    f"Пример ошибки из базы знаний (с чем сравнивалось):\n{example_text}"
                )
            parts.append(f"Описание: {entry.description}")
            if entry.resolution_steps:
                parts.append("Как исправить по базе знаний:")
                for step in entry.resolution_steps:
                    parts.append(f"  - {step}")

        # Подсказка для LLM: по каким данным был собран запрос к базе знаний.
        if kb_query_provenance:
            msg_len, trace_len, log_len = kb_query_provenance
            source_parts = []
            if msg_len > 0:
                source_parts.append(f"сообщение об ошибке ({msg_len} симв.)")
            if trace_len > 0:
                source_parts.append(f"стек-трейс ({trace_len} симв.)")
            if log_len > 0:
                source_parts.append(f"лог приложения ({log_len} симв.)")
            if source_parts:
                sources = " + ".join(source_parts)
                parts.append(
                    f"\nСовпадение из базы знаний найдено по объединённому тексту: {sources}. "
                    "Если сообщение об ошибке не похоже на пример из базы знаний, "
                    "проверь стек-трейс и лог — совпадение могло быть именно по ним."
                )

        # Для high-confidence совпадения из базы знаний всегда просим верификацию по данным кластера.
        top_match = kb_matches[0]
        if top_match.score >= 0.70:
            parts.append(
                "\n--- Инструкция по проверке записи базы знаний #1 ---\n"
                "Сравни «Пример ошибки из базы знаний» выше с «Сообщением об ошибке», "
                "«Стек-трейсом» и «Фрагментом лога» ниже.\n"
                "Сообщение об ошибке тест-фреймворка (assertion) часто отличается "
                "от корневой причины в логе приложения. Различие в формулировке "
                "сообщения об ошибке НЕ является противоречием совпадению из базы знаний.\n"
                "Противоречие — это когда данные ПРЯМО указывают на другую причину "
                "(другой сервис, другой тип ошибки, другой компонент)."
            )

    if cluster.example_message:
        msg = _truncate_prompt_text(
            cluster.example_message,
            _PROMPT_MESSAGE_MAX_CHARS,
        )
        parts.append(f"\n--- Сообщение об ошибке ---\n{msg}")

    trace_text = full_trace or cluster.example_trace_snippet
    if trace_text:
        trace_text = normalize_text_for_llm(trace_text)
        trace_text = _truncate_prompt_text(
            trace_text,
            _PROMPT_TRACE_MAX_CHARS,
        )
        parts.append(f"\n--- Стек-трейс ---\n{trace_text}")

    if log_snippet:
        log_text = normalize_text_for_llm(log_snippet)
        log_text = _truncate_prompt_text(
            log_text,
            _PROMPT_LOG_MAX_CHARS,
        )
        parts.append(f"\n--- Фрагмент лога ---\n{log_text}")

    parts.append("")
    parts.append("═══════════════════════════════════════")
    parts.append("ЗАДАНИЕ")
    parts.append("═══════════════════════════════════════")
    parts.append("")

    instruction = (
        "Верни ответ СТРОГО в формате ниже, без вступления и без markdown-заголовков:\n"
        "\n"
        "ЧТО СЛОМАЛОСЬ: 1-2 предложения. Если подходит запись базы знаний #1, начни объяснение с неё "
        "и затем добавь подтверждение из ошибки, трейса или лога.\n"
        "\n"
        "ПРИЧИНА: ровно одна категория из списка: тест / приложение / окружение / данные. "
        "Формат строки: '<категория> — <краткое обоснование>'. "
        "Если использована база знаний, обязательно назови запись базы знаний #1 или её название.\n"
        "\n"
        "КАК ИСПРАВИТЬ:\n"
        "1. Первый конкретный шаг исправления.\n"
        "2. Второй конкретный шаг исправления.\n"
        "3. Третий конкретный шаг исправления.\n"
        "\n"
        "ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЯ:\n"
    )

    if kb_matches:
        decision_rules = [
            "Сначала смотри запись базы знаний #1.",
            "Если у записи базы знаний #1 score >= 0.70 и нет прямого противоречия в данных — "
            "считай её основной версией причины и опирайся на её описание и шаги.",
            "Чтобы отклонить запись базы знаний #1 с score >= 0.70, ты ОБЯЗАН процитировать "
            "конкретную строку из ошибки, трейса или лога, которая ПРЯМО "
            "указывает на другую причину (другой сервис, другой тип сбоя, "
            "другой компонент). Различие в формулировке сообщения об ошибке "
            "НЕ является противоречием.",
            "Если у записи базы знаний #1 score 0.40-0.69 — используй её как рабочую гипотезу, "
            "но обязательно подтверди цитатой или фактом из ошибки, трейса или лога.",
            "Записи базы знаний #2 и #3 используй только как дополнительный контекст, "
            "если они не противоречат записи базы знаний #1.",
        ]
    else:
        decision_rules = [
            "Базы знаний нет — опирайся только на сообщение ошибки, трейс и лог.",
        ]

    decision_rules.extend([
        "Каждый шаг должен описывать, что именно нужно исправить, изменить, перезапустить, "
        "обновить или починить, и быть привязан к конкретике из ошибки, лога или базы знаний.",
        "Не пиши диагностические советы в стиле «проверьте» или «посмотрите», "
        "если из данных уже понятно, какое исправление требуется.",
        "Не давай абстрактных советов вроде «проверьте сервер» или «спросите команду».",
        "Не выдумывай новые причины, сервисы, конфиги, классы, методы или команды.",
    ])

    instruction += "".join(
        f"{index}. {rule}\n"
        for index, rule in enumerate(decision_rules, start=1)
    )
    instruction += (
        "\n"
        "Если данных мало — прямо напиши, что данных недостаточно, и не додумывай."
    )

    parts.append(instruction)

    return "\n".join(parts)


def build_launch_summary_prompt(
    clustering_report: ClusteringReport,
    triage_report: TriageReport,
    llm_result: LLMAnalysisResult | None = None,
) -> str:
    """Собрать промпт для итоговой LLM-сводки по всему прогону.

    Включает метаданные запуска, данные по каждому кластеру и (если доступны)
    per-cluster LLM-анализы. Просит LLM сформировать единый краткий отчёт.
    """
    parts: list[str] = [
        "Ты — инженер по анализу сбоев автотестов.",
        "Подготовь краткий итоговый отчёт по прогону тестов.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши только то, что видишь в данных ниже. "
        "Не додумывай, не предполагай. "
        "Если чего-то нет в данных — не упоминай это.",
        "",
        "═══════════════════════════════════════",
        "ДАННЫЕ",
        "═══════════════════════════════════════",
        "",
    ]

    launch_label = f"Запуск: #{triage_report.launch_id}"
    if triage_report.launch_name:
        launch_label += f" ({triage_report.launch_name})"
    parts.append(launch_label)
    parts.append(
        f"Всего тестов: {triage_report.total_results}"
        f" | Упало: {triage_report.failure_count}"
    )
    parts.append(
        f"Уникальных проблем (кластеров): {clustering_report.cluster_count}"
    )

    for i, cluster in enumerate(clustering_report.clusters, 1):
        parts.append("")
        parts.append(
            f"--- Проблема {i}: {cluster.label} ({cluster.member_count} тестов) ---"
        )

        # Используем per-cluster LLM-анализ, если есть
        if llm_result is not None:
            analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
            if analysis and analysis.analysis_text:
                parts.append(analysis.analysis_text)
                continue

        # Fallback: сырые данные кластера
        if cluster.example_step_path:
            parts.append(f"Шаг теста: {cluster.example_step_path}")
        if cluster.example_message:
            msg = cluster.example_message
            if len(msg) > 500:
                msg = msg[:500] + "...[обрезано]"
            parts.append(f"Сообщение: {msg}")
        if cluster.example_trace_snippet:
            trace = cluster.example_trace_snippet
            if len(trace) > 800:
                trace = trace[:800] + "...[обрезано]"
            parts.append(f"Трейс: {trace}")

    parts.append("")
    parts.append("═══════════════════════════════════════")
    parts.append("ЗАДАНИЕ")
    parts.append("═══════════════════════════════════════")
    parts.append("")
    parts.append(
        "Напиши итоговый отчёт в 2-4 абзаца:\n"
        "\n"
        "1. Общая картина: сколько тестов упало, сколько уникальных проблем выявлено.\n"
        "\n"
        "2. Ключевые проблемы: для каждой — одно предложение (что упало, "
        "почему, категория: тест / приложение / окружение / данные). "
        "Расставь по убыванию критичности и количества затронутых тестов.\n"
        "\n"
        "3. Приоритетные исправления: 1-3 конкретных шага на ближайшее время, "
        "что именно нужно исправить в коде, тесте, конфигурации, данных или окружении, "
        "опираясь только на данные выше. Пиши именно исправления, а не диагностику.\n"
        "\n"
        "Будь лаконичен. Избегай повторов. Не упоминай то, чего нет в данных."
    )

    return "\n".join(parts)


def format_llm_comment(
    analysis_text: str,
    *,
    step_path: str | None = None,
) -> str:
    """Отформатировать LLM-анализ в текст комментария для TestOps."""
    parts = [_LLM_HEADER, _SEPARATOR, ""]
    if step_path:
        parts.append(f"Шаг теста: {step_path}")
        parts.append("")
    parts.append(analysis_text)
    return "\n".join(parts)


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
        kb_provenance: dict[str, tuple[int, int, int]] | None = None,
    ) -> LLMAnalysisResult:
        """Проанализировать все кластеры через LLM.

        Args:
            clustering_report: Отчёт кластеризации.
            kb_results: Опционально — совпадения с базой знаний для обогащения промпта.
            failed_tests: Опционально — список тестов для извлечения log_snippet.
            kb_provenance: Опционально — (msg_len, trace_len, log_len) per cluster.

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

            kb_matches = (kb_results or {}).get(cluster.cluster_id)

            # Получить log_snippet и full_trace представителя (fallback на members)
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
            has_log_errors = has_explicit_errors(log_snippet) if has_log else False
            kb_count = len(kb_matches) if kb_matches else 0

            logger.info(
                "LLM: кластер %s (%d тестов) — "
                "лог отправлен: %s, ошибки в логе: %s, совпадений с базой знаний: %d",
                cluster.cluster_id[:8],
                cluster.member_count,
                "да" if has_log else "нет",
                "да" if has_log_errors else "нет",
                kb_count,
            )

            provenance = (kb_provenance or {}).get(cluster.cluster_id)
            prompt = build_cluster_prompt(
                cluster, kb_matches, log_snippet, full_trace,
                kb_query_provenance=provenance,
            )

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
            # Поле оставлено в API/CLI ответах для обратной совместимости.
            kb_bypass_count=0,
            cluster_analyses=analyses,
        )

    async def generate_launch_summary(
        self,
        clustering_report: ClusteringReport,
        triage_report: TriageReport,
        llm_result: LLMAnalysisResult | None = None,
    ) -> LLMLaunchSummary:
        """Сформировать итоговый отчёт по прогону через LLM.

        Строит промпт из данных всех кластеров (и per-cluster анализов, если
        они доступны) и делает один LLM-вызов для получения единой сводки.

        Args:
            clustering_report: Отчёт кластеризации.
            triage_report: Отчёт триажа (метаданные запуска).
            llm_result: Опционально — per-cluster анализы для обогащения промпта.

        Returns:
            LLMLaunchSummary с текстом итогового отчёта.
        """
        prompt = build_launch_summary_prompt(clustering_report, triage_report, llm_result)
        logger.info(
            "LLM summary: запрос итогового отчёта по %d кластерам",
            clustering_report.cluster_count,
        )
        try:
            text = await self._client.run_flow(prompt)
            logger.info("LLM summary: отчёт получен (%d символов)", len(text))
            return LLMLaunchSummary(summary_text=text)
        except Exception as exc:
            logger.warning("LLM summary: ошибка: %s", exc)
            return LLMLaunchSummary(summary_text="", error=str(exc))


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

        comment_text = format_llm_comment(
            analysis.analysis_text,
            step_path=cluster.example_step_path,
        )

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
