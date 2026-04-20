"""Сервис LLM-анализа кластеров ошибок через GigaChat."""

import asyncio
import logging
from typing import Protocol

from alla.clients.base import TestResultsUpdater
from alla.clients.gigachat_client import ChatResponse
from alla.knowledge.models import KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.llm import (
    LLMAnalysisResult,
    LLMClusterAnalysis,
    LLMLaunchSummary,
    LLMPushResult,
    TokenUsage,
)
from alla.models.testops import FailedTestSummary, TriageReport
from alla.utils.log_utils import has_explicit_errors
from alla.utils.text_normalization import normalize_text_for_llm

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Протокол LLM-клиента для LLMService."""

    async def chat(self, system_prompt: str, user_prompt: str) -> ChatResponse: ...


_LLM_HEADER = "[alla] LLM-анализ ошибки"
_SEPARATOR = "=" * 40
_EXACT_KB_SCORE = 0.999  # Нужен для метки EXACT MATCH в промпте.
_PROMPT_MESSAGE_MAX_CHARS = 2000
_PROMPT_TRACE_MAX_CHARS = 400
_PROMPT_LOG_MAX_CHARS = 8000
_TRUNCATION_SUFFIX = "...[обрезано]"

def _interpret_kb_score(score: float) -> str:
    """Перевести score совпадения с базой знаний в текстовое описание уверенности."""
    if score >= 0.999:
        return "точное совпадение"
    if score >= 0.85:
        return "высокое совпадение"
    if score >= 0.65:
        return "среднее совпадение"
    if score >= 0.4:
        return "слабое совпадение"
    return "очень слабое совпадение"


def _match_tier_tag(match: KBMatchResult) -> str:
    """Короткий тег механизма совпадения для заголовка в промпте."""
    if match.match_origin == "feedback_exact":
        return "feedback_exact"
    for reason in match.matched_on or []:
        if "Tier 1" in reason or "exact substring" in reason.lower():
            return "Tier 1 (точное)"
        if "Tier 2" in reason or "line match" in reason.lower():
            return "Tier 2 (построчное)"
        if "Tier 3" in reason or "TF-IDF" in reason:
            return "Tier 3 (нечёткое TF-IDF)"
    return "неизвестный механизм"


def _format_kb_category(category: RootCauseCategory) -> str:
    """Перевести категорию записи из базы знаний в формулировку для LLM."""
    mapping = {
        RootCauseCategory.TEST: "тест",
        RootCauseCategory.SERVICE: "приложение",
        RootCauseCategory.ENV: "окружение",
        RootCauseCategory.DATA: "данные",
    }
    return mapping.get(category, category.value)


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
    message_max_chars: int = _PROMPT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = _PROMPT_TRACE_MAX_CHARS,
    log_max_chars: int = _PROMPT_LOG_MAX_CHARS,
) -> tuple[str, str]:
    """Собрать промпт для LLM-анализа одного кластера.

    Возвращает ``(system_prompt, user_prompt)``.

    System: роль и правила поведения.
    User: данные кластера + база знаний + задание.
    """
    system_parts: list[str] = [
        "Ты — инженер по анализу сбоев автотестов.",
        "Твоя задача: быстро и точно определить причину падения и дать полезные шаги исправления.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши ТОЛЬКО то, что видишь в данных ниже. "
        "Не додумывай, не предполагай, не фантазируй. "
        "Если чего-то нет в ошибке, стек-трейсе, логе или базе знаний — не упоминай это.",
        "",
        "ПРИОРИТЕТ ИСТОЧНИКОВ:",
        "1. Сообщение ошибки / стек-трейс / лог — это первичные факты. На них строится анализ.",
        "2. БАЗА ЗНАНИЙ — справочник похожих инцидентов. Её запись можно использовать ТОЛЬКО если "
        "ты докажешь её применимость к конкретным данным кластера: процитируешь фрагмент из "
        "«Пример ошибки из базы знаний», который дословно или близко присутствует в сообщении "
        "ошибки / трейсе / логе кластера.",
        "3. Только точное совпадение (Tier 1 / feedback_exact / score >= 0.999) можно использовать "
        "как обязательную основную причину без дополнительной проверки.",
        "4. Высокий score (0.65-0.99) сам по себе НЕ доказывает применимость: он может "
        "отражать лишь совпадение по общим словам (assertion, error, failed, timeout, 500, null). "
        "Если семантической переклички нет — отвергай запись, даже при score 0.95.",
        "5. Если ни одна запись не прошла проверку применимости — прямо сообщи, что подходящих "
        "записей в базе знаний нет, и строй анализ только по ошибке / трейсу / логу.",
    ]

    parts: list[str] = [
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
        parts.append("\n--- База знаний (справочник, НЕ готовый ответ) ---")
        if _is_exact_kb_match(kb_matches[0]):
            parts.append(
                "ТОЧНОЕ СОВПАДЕНИЕ: запись базы знаний #1 — Tier 1 / feedback_exact "
                "(score ~1.00). Это обязательная основная причина; записи базы знаний #2 и #3 "
                "игнорируй, если нет прямого противоречия в данных."
            )
        parts.append(
            "Правила использования записей с неточным совпадением:\n"
            "  • score >= 0.999 ИЛИ Tier 1 / feedback_exact → обязательная основная причина.\n"
            "  • score 0.65-0.999 → КАНДИДАТ. Принять можно ТОЛЬКО если ты процитируешь "
            "конкретный фрагмент из «Пример ошибки из базы знаний», который дословно или "
            "очень близко присутствует в сообщении ошибки / трейсе / логе кластера. "
            "Если пересечение — только по общим словам (assertion, error, failed, timeout, "
            "null, 500, exception) без контекста компонента и типа сбоя — ОТКАЗАТЬСЯ.\n"
            "  • score 0.40-0.65 → слабая гипотеза. Использовать только при явной "
            "дословной переклички; в остальных случаях игнорировать.\n"
            "  • score < 0.40 → не использовать; максимум упомянуть как «возможно связано»."
        )
        parts.append(
            "Высокий score сам по себе НЕ доказательство применимости. Tier 2 (построчное) "
            "и Tier 3 (TF-IDF) легко дают 0.70-0.95 на совпадении служебных строк. "
            "Доказательство — только дословная/фразовая перекличка по сути сбоя."
        )
        for index, m in enumerate(kb_matches[:3], start=1):
            entry = m.entry
            confidence = _interpret_kb_score(m.score)
            tier_tag = _match_tier_tag(m)
            if index == 1 and _is_exact_kb_match(m):
                role = "exact match; обязательная основная причина"
            elif index == 1:
                role = "кандидат — требует проверки применимости"
            else:
                role = "дополнительный кандидат — требует проверки применимости"
            parts.append(
                f"\nСовпадение из базы знаний #{index} "
                f"[{role}; {tier_tag}; {confidence}; score {m.score:.2f}]"
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

        # Для неточных совпадений из базы знаний требуем позитивное доказательство применимости.
        top_match = kb_matches[0]
        if not _is_exact_kb_match(top_match):
            parts.append(
                "\n--- Инструкция по проверке применимости записи базы знаний #1 ---\n"
                "Сравни «Пример ошибки из базы знаний» выше с «Сообщением об ошибке», "
                "«Стек-трейсом» и «Фрагментом лога» ниже. Действуй так:\n"
                "1) Найди конкретный фрагмент (подстроку или близкую перефразировку) "
                "из «Пример ошибки из базы знаний», который присутствует в данных кластера "
                "и описывает тот же компонент / тот же тип сбоя, а не общий шум "
                "(assertion, error, failed, timeout, null, 500, stack trace).\n"
                "2) Если такой фрагмент нашёлся — процитируй его дословно в поле "
                "«ЧТО СЛОМАЛОСЬ» как подтверждение и используй запись как основную причину.\n"
                "3) Если совпадают только общие слова или служебные строки без "
                "семантической переклички — НЕ используй запись, даже если score высокий. "
                "Прямо напиши: «запись базы знаний #1 не подходит: совпадение по общим "
                "словам, нет переклички по сути сбоя» и строй анализ только по ошибке, "
                "трейсу и логу.\n"
                "4) Бремя доказательства применимости на тебе. Высокий score ≠ применимость."
            )

    if cluster.example_message:
        msg = _truncate_prompt_text(
            cluster.example_message,
            message_max_chars,
        )
        parts.append(f"\n--- Сообщение об ошибке ---\n{msg}")

    trace_text = full_trace or cluster.example_trace_snippet
    if trace_text:
        trace_text = normalize_text_for_llm(trace_text)
        trace_text = _truncate_prompt_text(
            trace_text,
            trace_max_chars,
        )
        parts.append(f"\n--- Стек-трейс ---\n{trace_text}")

    if log_snippet:
        log_text = normalize_text_for_llm(log_snippet)
        log_text = _truncate_prompt_text(
            log_text,
            log_max_chars,
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
            "Определи механизм совпадения записи базы знаний #1 по тегу tier. "
            "Если это Tier 1 / feedback_exact (score >= 0.999) — запись применима безусловно, "
            "используй её как основную причину.",
            "Для всех остальных записей ответь себе на вопрос: «Совпадает ли "
            "«Пример ошибки из базы знаний» по сути с данными кластера — тот же компонент, "
            "тот же тип сбоя, те же ключевые фразы?»\n"
            "   • ДА (есть дословный или близкий фрагмент, кроме общих слов) — процитируй "
            "этот фрагмент в поле «ЧТО СЛОМАЛОСЬ» и используй запись.\n"
            "   • НЕТ — НЕ используй запись, даже если score 0.79, 0.85 или 0.95. Выведи анализ "
            "только по сообщению ошибки, трейсу и логу.",
            "При score 0.65-0.85 строгая презумпция — запись НЕ применима, пока ты не "
            "нашёл и не процитировал конкретный фрагмент из «Пример ошибки из базы знаний» в "
            "данных кластера. Высокого score по Tier 2 (построчное) или Tier 3 (TF-IDF) "
            "самого по себе НЕДОСТАТОЧНО.",
            "Если ни одна запись не прошла проверку применимости — прямо напиши "
            "«подходящих записей в базе знаний нет» и строй анализ без них. Не подгоняй.",
            "Записи базы знаний #2 и #3 — только как дополнительный контекст и только "
            "если прошли ту же проверку применимости, что и запись #1.",
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

    return "\n".join(system_parts), "\n".join(parts)


def build_launch_summary_prompt(
    clustering_report: ClusteringReport,
    triage_report: TriageReport,
    llm_result: LLMAnalysisResult | None = None,
) -> tuple[str, str]:
    """Собрать промпт для итоговой LLM-сводки по всему прогону.

    Возвращает ``(system_prompt, user_prompt)``.
    """
    system_parts: list[str] = [
        "Ты — инженер по анализу сбоев автотестов.",
        "Подготовь краткий итоговый отчёт по прогону тестов.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши только то, что видишь в данных ниже. "
        "Не додумывай, не предполагай. "
        "Если чего-то нет в данных — не упоминай это.",
    ]

    parts: list[str] = [
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

    return "\n".join(system_parts), "\n".join(parts)


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
    """Анализ кластеров ошибок через LLM.

    Для каждого кластера: строит промпт, вызывает LLM, сохраняет результат.
    Между запросами выдерживается минимальный интервал ``request_delay``
    для предотвращения 429 ошибок от GigaChat API.
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
        self, system_prompt: str, user_prompt: str,
    ) -> ChatResponse:
        """Вызвать LLM с соблюдением минимального интервала между запросами.

        Lock удерживается только на время вычисления паузы и обновления
        timestamp. Сетевой запрос выполняется после отпускания lock,
        чтобы не сериализовать параллельные запросы.
        """
        if self._request_delay > 0:
            async with self._rate_lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                elapsed = now - self._last_request_time
                if self._last_request_time > 0 and elapsed < self._request_delay:
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

        # Индекс test_result_id → FailedTestSummary для быстрого lookup
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
            system_prompt, user_prompt = build_cluster_prompt(
                cluster, kb_matches, log_snippet, full_trace,
                kb_query_provenance=provenance,
                message_max_chars=self._message_max_chars,
                trace_max_chars=self._trace_max_chars,
                log_max_chars=self._log_max_chars,
            )

            async with semaphore:
                try:
                    chat_response = await self._rate_limited_chat(system_prompt, user_prompt)
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
        """Сформировать итоговый отчёт по прогону через LLM.

        Строит промпт из данных всех кластеров (и per-cluster анализов, если
        они доступны) и делает один LLM-вызов для получения единой сводки.
        """
        system_prompt, user_prompt = build_launch_summary_prompt(
            clustering_report, triage_report, llm_result,
        )
        logger.info(
            "LLM summary: запрос итогового отчёта по %d кластерам",
            clustering_report.cluster_count,
        )
        try:
            chat_response = await self._rate_limited_chat(system_prompt, user_prompt)
            logger.info("LLM summary: отчёт получен (%d символов)", len(chat_response.text))
            return LLMLaunchSummary(
                summary_text=chat_response.text,
                token_usage=chat_response.token_usage,
            )
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
