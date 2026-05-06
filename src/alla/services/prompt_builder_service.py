"""Публичный сервис сборки промптов для анализа кластеров и summary прогона.

Один источник истины и для server-side LLM-пути (GigaChat в
:mod:`alla.services.llm_service`), и для skill-режима, где промпт
исполняет агент CLI.

Основные функции:

* :func:`build_cluster_analysis_prompt` — промпт анализа одного кластера.
* :func:`build_launch_summary_prompt` — промпт итогового отчёта по прогону.

Возвращают :class:`ClusterAnalysisPrompt` и :class:`LaunchSummaryPrompt`,
содержащие ``system_prompt`` и ``user_prompt`` плюс summary входных данных
для отладки/observability.
"""

from __future__ import annotations

from dataclasses import dataclass

from alla.knowledge.models import KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.llm import LLMAnalysisResult
from alla.models.testops import TriageReport
from alla.utils.text_normalization import normalize_text_for_llm

__all__ = [
    "ClusterAnalysisPrompt",
    "LaunchSummaryPrompt",
    "build_cluster_analysis_prompt",
    "build_launch_summary_prompt",
    "DEFAULT_MESSAGE_MAX_CHARS",
    "DEFAULT_TRACE_MAX_CHARS",
    "DEFAULT_LOG_MAX_CHARS",
]

DEFAULT_MESSAGE_MAX_CHARS = 2000
DEFAULT_TRACE_MAX_CHARS = 400
DEFAULT_LOG_MAX_CHARS = 8000

_EXACT_KB_SCORE = 0.999
_TRUNCATION_SUFFIX = "...[обрезано]"


@dataclass(frozen=True)
class ClusterAnalysisPrompt:
    """Готовый промпт для анализа одного кластера.

    Атрибуты:
        system_prompt: системный промпт (роль и правила поведения).
        user_prompt: пользовательский промпт (данные + задание).
        message_chars: фактическая длина сообщения после truncation.
        trace_chars: фактическая длина трейса после truncation.
        log_chars: фактическая длина лога после truncation.
        kb_match_count: число KB-совпадений, попавших в промпт.
    """

    system_prompt: str
    user_prompt: str
    message_chars: int
    trace_chars: int
    log_chars: int
    kb_match_count: int


@dataclass(frozen=True)
class LaunchSummaryPrompt:
    """Готовый промпт для итогового отчёта по прогону."""

    system_prompt: str
    user_prompt: str
    cluster_count: int
    analyses_used: int


# ---------------------------------------------------------------------------
# Cluster analysis prompt
# ---------------------------------------------------------------------------


def build_cluster_analysis_prompt(
    cluster: FailureCluster,
    kb_matches: list[KBMatchResult] | None = None,
    log_snippet: str | None = None,
    full_trace: str | None = None,
    *,
    kb_query_provenance: tuple[int, int, int] | None = None,
    message_max_chars: int = DEFAULT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = DEFAULT_TRACE_MAX_CHARS,
    log_max_chars: int = DEFAULT_LOG_MAX_CHARS,
) -> ClusterAnalysisPrompt:
    """Собрать промпт для анализа одного кластера.

    Поведение совпадает с прежним ``build_cluster_prompt`` из
    :mod:`alla.services.llm_service` — это единственный builder, который
    теперь используют и LLM-сервис, и скрипты skill.
    """
    system_prompt = _SYSTEM_PROMPT_CLUSTER

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

    kb_count = len(kb_matches) if kb_matches else 0
    if kb_matches:
        _append_kb_section(parts, kb_matches, kb_query_provenance)

    message_chars = 0
    if cluster.example_message:
        msg = _truncate_prompt_text(cluster.example_message, message_max_chars)
        message_chars = len(msg)
        parts.append(f"\n--- Сообщение об ошибке ---\n{msg}")

    trace_text = full_trace or cluster.example_trace_snippet
    trace_chars = 0
    if trace_text:
        trace_text = normalize_text_for_llm(trace_text)
        trace_text = _truncate_prompt_text(trace_text, trace_max_chars)
        trace_chars = len(trace_text)
        parts.append(f"\n--- Стек-трейс ---\n{trace_text}")

    log_chars = 0
    if log_snippet:
        log_text = normalize_text_for_llm(log_snippet)
        log_text = _truncate_prompt_text(log_text, log_max_chars)
        log_chars = len(log_text)
        parts.append(f"\n--- Фрагмент лога ---\n{log_text}")

    if message_chars > 0 and log_chars > 0:
        parts.append(
            "\nПодсказка: «Сообщение об ошибке» — это реакция теста "
            "(симптом), «Фрагмент лога» — поведение приложения "
            "(часто первопричина). Свяжи их в анализе."
        )

    parts.append("")
    parts.append("═══════════════════════════════════════")
    parts.append("ЗАДАНИЕ")
    parts.append("═══════════════════════════════════════")
    parts.append("")
    parts.append(
        _build_cluster_instruction(kb_matches, has_log=log_chars > 0)
    )

    return ClusterAnalysisPrompt(
        system_prompt=system_prompt,
        user_prompt="\n".join(parts),
        message_chars=message_chars,
        trace_chars=trace_chars,
        log_chars=log_chars,
        kb_match_count=kb_count,
    )


# ---------------------------------------------------------------------------
# Launch summary prompt
# ---------------------------------------------------------------------------


def build_launch_summary_prompt(
    clustering_report: ClusteringReport,
    triage_report: TriageReport,
    llm_result: LLMAnalysisResult | None = None,
) -> LaunchSummaryPrompt:
    """Собрать промпт итогового отчёта по прогону.

    Поведение совпадает с прежним ``build_launch_summary_prompt`` из
    :mod:`alla.services.llm_service`. ``llm_result`` принимается как
    обобщённый источник per-cluster анализа (для skill это
    адаптированный результат агентского анализа).
    """
    system_prompt = _SYSTEM_PROMPT_SUMMARY

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

    analyses_used = 0
    for index, cluster in enumerate(clustering_report.clusters, 1):
        parts.append("")
        parts.append(
            f"--- Проблема {index}: {cluster.label} "
            f"({cluster.member_count} тестов) ---"
        )
        if llm_result is not None:
            analysis = llm_result.cluster_analyses.get(cluster.cluster_id)
            if analysis and analysis.analysis_text:
                parts.append(analysis.analysis_text)
                analyses_used += 1
                continue

        if cluster.example_step_path:
            parts.append(f"Шаг теста: {cluster.example_step_path}")
        if cluster.example_message:
            msg = cluster.example_message
            if len(msg) > 500:
                msg = msg[:500] + _TRUNCATION_SUFFIX
            parts.append(f"Сообщение: {msg}")
        if cluster.example_trace_snippet:
            trace = cluster.example_trace_snippet
            if len(trace) > 800:
                trace = trace[:800] + _TRUNCATION_SUFFIX
            parts.append(f"Трейс: {trace}")

    parts.append("")
    parts.append("═══════════════════════════════════════")
    parts.append("ЗАДАНИЕ")
    parts.append("═══════════════════════════════════════")
    parts.append("")
    parts.append(_LAUNCH_SUMMARY_INSTRUCTION)

    return LaunchSummaryPrompt(
        system_prompt=system_prompt,
        user_prompt="\n".join(parts),
        cluster_count=clustering_report.cluster_count,
        analyses_used=analyses_used,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_CLUSTER = "\n".join(
    [
        "Ты — инженер по анализу сбоев автотестов.",
        "Твоя задача: быстро и точно определить причину падения и дать "
        "полезные шаги исправления.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши ТОЛЬКО то, что видишь в данных ниже. "
        "Не додумывай, не предполагай, не фантазируй. "
        "Если чего-то нет в ошибке, стек-трейсе, логе или базе знаний — "
        "не упоминай это.",
        "",
        "ПРИОРИТЕТ ИСТОЧНИКОВ:",
        "1. Первичные факты — это ДВА равноправных источника, которые нужно "
        "использовать вместе:",
        "   • Сообщение об ошибке и стек-трейс — фиксируют СИМПТОМ, который "
        "увидел тест (assertion, HTTP-код, исключение в клиентском коде). "
        "Это реакция теста на поведение системы.",
        "   • Фрагмент лога приложения — фиксирует ПОВЕДЕНИЕ самого "
        "приложения в момент падения и часто содержит первопричину "
        "(серверный stack trace, ошибка БД, таймаут зависимости, "
        "бизнес-валидация, исключение в сервисе).",
        "2. Если в данных кластера присутствуют ОБА источника — анализ "
        "обязан опираться на оба и связать симптом теста с поведением "
        "приложения из лога. Нельзя выдавать вердикт только по сообщению "
        "автотеста, игнорируя лог.",
        "3. Если фрагмент лога приложения содержит явную ошибку / "
        "исключение / серверный стек — она имеет ПРИОРИТЕТ при определении "
        "первопричины: лог описывает поведение системы, а ошибка теста — "
        "лишь реакцию клиента на это поведение.",
        "4. Если лог пуст или не содержит признаков ошибки — прямо "
        "зафиксируй это в анализе («лог приложения пуст / без явных "
        "ошибок») и строй вывод только по сообщению ошибки и трейсу.",
        "5. БАЗА ЗНАНИЙ — справочник похожих инцидентов. Её запись можно "
        "использовать ТОЛЬКО если ты докажешь её применимость к конкретным "
        "данным кластера: процитируешь фрагмент из «Пример ошибки из базы "
        "знаний», который дословно или близко присутствует в сообщении "
        "ошибки / трейсе / логе кластера.",
        "6. Только точное совпадение по механизму Tier 1 / feedback_exact "
        "можно использовать как обязательную основную причину без "
        "дополнительной проверки. Высокий score (в т.ч. 0.999+) без такого "
        "механизма точным совпадением НЕ считается.",
        "7. Высокий score (0.65-0.99) сам по себе НЕ доказывает применимость: "
        "он может отражать лишь совпадение по общим словам "
        "(assertion, error, failed, timeout, 500, null). Если семантической "
        "переклички нет — отвергай запись, даже при score 0.95.",
        "8. Если ни одна запись не прошла проверку применимости — прямо "
        "сообщи, что подходящих записей в базе знаний нет, и строй анализ "
        "только по ошибке / трейсу / логу.",
    ]
)


_SYSTEM_PROMPT_SUMMARY = "\n".join(
    [
        "Ты — инженер по анализу сбоев автотестов.",
        "Подготовь краткий итоговый отчёт по прогону тестов.",
        "",
        "ГЛАВНОЕ ПРАВИЛО: пиши только то, что видишь в данных ниже. "
        "Не додумывай, не предполагай. "
        "Если чего-то нет в данных — не упоминай это.",
    ]
)


_LAUNCH_SUMMARY_INSTRUCTION = (
    "Напиши итоговый отчёт в 2-4 абзаца:\n"
    "\n"
    "1. Общая картина: сколько тестов упало, сколько уникальных проблем "
    "выявлено.\n"
    "\n"
    "2. Ключевые проблемы: для каждой — одно предложение (что упало, "
    "почему, категория: тест / приложение / окружение / данные). "
    "Расставь по убыванию критичности и количества затронутых тестов.\n"
    "\n"
    "3. Приоритетные исправления: 1-3 конкретных шага на ближайшее время, "
    "что именно нужно исправить в коде, тесте, конфигурации, данных или "
    "окружении, опираясь только на данные выше. Пиши именно исправления, "
    "а не диагностику.\n"
    "\n"
    "Будь лаконичен. Избегай повторов. Не упоминай то, чего нет в данных."
)


def _truncate_prompt_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_SUFFIX


def _format_kb_category(category: RootCauseCategory) -> str:
    mapping = {
        RootCauseCategory.TEST: "тест",
        RootCauseCategory.SERVICE: "приложение",
        RootCauseCategory.ENV: "окружение",
        RootCauseCategory.DATA: "данные",
    }
    return mapping.get(category, category.value)


def _is_exact_kb_match(match: KBMatchResult) -> bool:
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


def _interpret_kb_match(match: KBMatchResult) -> str:
    if _is_exact_kb_match(match):
        return "точное совпадение"
    score = match.score
    if score >= 0.85:
        return "высокое совпадение"
    if score >= 0.65:
        return "среднее совпадение"
    if score >= 0.4:
        return "слабое совпадение"
    return "очень слабое совпадение"


def _match_tier_tag(match: KBMatchResult) -> str:
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


def _humanize_match_reason(matched_on: list[str]) -> str:
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


def _append_kb_section(
    parts: list[str],
    kb_matches: list[KBMatchResult],
    kb_query_provenance: tuple[int, int, int] | None,
) -> None:
    parts.append("\n--- База знаний (справочник, НЕ готовый ответ) ---")
    if _is_exact_kb_match(kb_matches[0]):
        parts.append(
            "ТОЧНОЕ СОВПАДЕНИЕ: запись базы знаний #1 — Tier 1 / "
            "feedback_exact (score ~1.00). Это обязательная основная причина; "
            "записи базы знаний #2 и #3 игнорируй, если нет прямого "
            "противоречия в данных."
        )
    parts.append(
        "Правила использования записей с неточным совпадением:\n"
        "  • Механизм Tier 1 / feedback_exact (точное совпадение) → "
        "обязательная основная причина. Высокий score без этого механизма "
        "сюда НЕ попадает.\n"
        "  • score 0.65-0.999 без точного механизма → КАНДИДАТ. Принять "
        "можно ТОЛЬКО если ты процитируешь конкретный фрагмент из «Пример "
        "ошибки из базы знаний», который дословно или очень близко "
        "присутствует в сообщении ошибки / трейсе / логе кластера. "
        "Если пересечение — только по общим словам (assertion, error, "
        "failed, timeout, null, 500, exception) без контекста компонента "
        "и типа сбоя — ОТКАЗАТЬСЯ.\n"
        "  • score 0.40-0.65 → слабая гипотеза. Использовать только при "
        "явной дословной переклички; в остальных случаях игнорировать.\n"
        "  • score < 0.40 → не использовать; максимум упомянуть "
        "как «возможно связано»."
    )
    parts.append(
        "Высокий score сам по себе НЕ доказательство применимости. "
        "Tier 2 (построчное) и Tier 3 (TF-IDF) легко дают 0.70-0.95 на "
        "совпадении служебных строк. Доказательство — только дословная/"
        "фразовая перекличка по сути сбоя."
    )

    for index, m in enumerate(kb_matches[:3], start=1):
        entry = m.entry
        confidence = _interpret_kb_match(m)
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
                example_text = example_text[:500] + _TRUNCATION_SUFFIX
            parts.append(
                f"Пример ошибки из базы знаний (с чем сравнивалось):\n{example_text}"
            )
        parts.append(f"Описание: {entry.description}")
        if entry.resolution_steps:
            parts.append("Как исправить по базе знаний:")
            for step in entry.resolution_steps:
                parts.append(f"  - {step}")

    if kb_query_provenance:
        msg_len, trace_len, log_len = kb_query_provenance
        source_parts: list[str] = []
        if msg_len > 0:
            source_parts.append(f"сообщение об ошибке ({msg_len} симв.)")
        if trace_len > 0:
            source_parts.append(f"стек-трейс ({trace_len} симв.)")
        if log_len > 0:
            source_parts.append(f"лог приложения ({log_len} симв.)")
        if source_parts:
            sources = " + ".join(source_parts)
            parts.append(
                f"\nСовпадение из базы знаний найдено по объединённому "
                f"тексту: {sources}. Если сообщение об ошибке не похоже "
                f"на пример из базы знаний, проверь стек-трейс и лог — "
                f"совпадение могло быть именно по ним."
            )

    top_match = kb_matches[0]
    if not _is_exact_kb_match(top_match):
        parts.append(
            "\n--- Инструкция по проверке применимости записи базы знаний "
            "#1 ---\n"
            "Сравни «Пример ошибки из базы знаний» выше с «Сообщением об "
            "ошибке», «Стек-трейсом» и «Фрагментом лога» ниже. Действуй "
            "так:\n"
            "1) Найди конкретный фрагмент (подстроку или близкую "
            "перефразировку) из «Пример ошибки из базы знаний», который "
            "присутствует в данных кластера и описывает тот же компонент / "
            "тот же тип сбоя, а не общий шум (assertion, error, failed, "
            "timeout, null, 500, stack trace).\n"
            "2) Если такой фрагмент нашёлся — процитируй его дословно в "
            "поле «ЧТО СЛОМАЛОСЬ» как подтверждение и используй запись "
            "как основную причину.\n"
            "3) Если совпадают только общие слова или служебные строки "
            "без семантической переклички — НЕ используй запись, даже "
            "если score высокий. Прямо напиши: «запись базы знаний #1 "
            "не подходит: совпадение по общим словам, нет переклички по "
            "сути сбоя» и строй анализ только по ошибке, трейсу и логу.\n"
            "4) Бремя доказательства применимости на тебе. "
            "Высокий score ≠ применимость."
        )


def _build_cluster_instruction(
    kb_matches: list[KBMatchResult] | None,
    *,
    has_log: bool = False,
) -> str:
    if has_log:
        what_broke = (
            "ЧТО СЛОМАЛОСЬ: 2 предложения. Первое — симптом из "
            "сообщения об ошибке / трейса (что увидел тест: assertion, "
            "HTTP-код, исключение в клиенте). Второе — первопричина из "
            "фрагмента лога приложения с дословной цитатой ключевой "
            "строки лога (исключение, ошибка БД, сообщение сервиса). "
            "Если подходит запись базы знаний #1 — начни с неё, затем "
            "добавь и подтверждение из лога, и симптом из ошибки.\n"
        )
        cause = (
            "ПРИЧИНА: ровно одна категория из списка: "
            "тест / приложение / окружение / данные. "
            "Формат строки: '<категория> — <краткое обоснование>'. "
            "Категорию выбирай по первопричине из лога приложения, "
            "а не по тексту assertion из теста. Например: "
            "assertEquals 200 != 500 + серверный stack trace в логе → "
            "«приложение»; тот же assertion без ошибок в логе и при "
            "наличии явной проблемы в самом тесте → «тест»; "
            "ConnectionRefused / DNS / таймаут в логе → «окружение»; "
            "ошибка валидации входных данных в логе → «данные». "
            "Если использована база знаний, обязательно назови запись "
            "базы знаний #1 или её название.\n"
        )
        fix = (
            "КАК ИСПРАВИТЬ:\n"
            "1. Первый конкретный шаг исправления — обязательно "
            "со ссылкой на конкретику из лога приложения "
            "(класс / метод / сервис / запрос из лога).\n"
            "2. Второй конкретный шаг исправления.\n"
            "3. Третий конкретный шаг исправления.\n"
        )
    else:
        what_broke = (
            "ЧТО СЛОМАЛОСЬ: 1-2 предложения по сообщению ошибки и "
            "трейсу. В начале явно укажи: «лог приложения пуст / без "
            "явных ошибок», поэтому анализ построен только по ошибке "
            "теста. Если подходит запись базы знаний #1, начни "
            "объяснение с неё и затем добавь подтверждение из ошибки "
            "или трейса.\n"
        )
        cause = (
            "ПРИЧИНА: ровно одна категория из списка: "
            "тест / приложение / окружение / данные. "
            "Формат строки: '<категория> — <краткое обоснование>'. "
            "Поскольку лог приложения пуст, категорию определи по "
            "сообщению ошибки и трейсу. "
            "Если использована база знаний, обязательно назови запись "
            "базы знаний #1 или её название.\n"
        )
        fix = (
            "КАК ИСПРАВИТЬ:\n"
            "1. Первый конкретный шаг исправления.\n"
            "2. Второй конкретный шаг исправления.\n"
            "3. Третий конкретный шаг исправления.\n"
        )

    instruction = (
        "Верни ответ СТРОГО в формате ниже, без вступления и без "
        "markdown-заголовков:\n"
        "\n"
        + what_broke
        + "\n"
        + cause
        + "\n"
        + fix
        + "\n"
        "ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЯ:\n"
    )

    decision_rules: list[str] = []
    if has_log:
        decision_rules.append(
            "Сначала прочитай и сообщение об ошибке, и фрагмент лога "
            "приложения. Сопоставь их: ошибка теста — симптом (что "
            "увидел клиент), лог приложения — поведение системы. "
            "Игнорировать лог при наличии в нём явной ошибки / "
            "исключения / стека НЕЛЬЗЯ. Если в логе видна серверная "
            "ошибка — она и есть первопричина."
        )
    else:
        decision_rules.append(
            "Лога приложения нет (или он пуст / без явных ошибок). "
            "Опирайся на сообщение ошибки и трейс. Не выдумывай "
            "содержимое лога."
        )

    if kb_matches:
        decision_rules.extend(
            [
                "Определи механизм совпадения записи базы знаний #1 по "
                "тегу tier. Если это Tier 1 / feedback_exact — запись "
                "применима безусловно, используй её как основную "
                "причину. Высокий score без этого механизма точным "
                "совпадением не считается — переходи к проверке "
                "применимости.",
                "Для всех остальных записей ответь себе на вопрос: "
                "«Совпадает ли «Пример ошибки из базы знаний» по сути "
                "с данными кластера — тот же компонент, тот же тип "
                "сбоя, те же ключевые фразы?»\n"
                "   • ДА (есть дословный или близкий фрагмент, кроме "
                "общих слов) — процитируй этот фрагмент в поле «ЧТО "
                "СЛОМАЛОСЬ» и используй запись.\n"
                "   • НЕТ — НЕ используй запись, даже если score 0.79, "
                "0.85 или 0.95. Выведи анализ только по сообщению "
                "ошибки, трейсу и логу.",
                "При score 0.65-0.85 строгая презумпция — запись НЕ "
                "применима, пока ты не нашёл и не процитировал "
                "конкретный фрагмент из «Пример ошибки из базы "
                "знаний» в данных кластера. Высокого score по Tier 2 "
                "(построчное) или Tier 3 (TF-IDF) самого по себе "
                "НЕДОСТАТОЧНО.",
                "Если ни одна запись не прошла проверку применимости — "
                "прямо напиши «подходящих записей в базе знаний нет» "
                "и строй анализ без них. Не подгоняй.",
                "Записи базы знаний #2 и #3 — только как "
                "дополнительный контекст и только если прошли ту же "
                "проверку применимости, что и запись #1.",
            ]
        )
    else:
        decision_rules.append(
            "Базы знаний нет — опирайся только на сообщение ошибки, "
            "трейс и лог."
        )

    decision_rules.extend(
        [
            "Каждый шаг должен описывать, что именно нужно исправить, "
            "изменить, перезапустить, обновить или починить, и быть "
            "привязан к конкретике из ошибки, лога или базы знаний.",
            "Не пиши диагностические советы в стиле «проверьте» или "
            "«посмотрите», если из данных уже понятно, какое исправление "
            "требуется.",
            "Не давай абстрактных советов вроде «проверьте сервер» или "
            "«спросите команду».",
            "Не выдумывай новые причины, сервисы, конфиги, классы, методы "
            "или команды.",
        ]
    )

    instruction += "".join(
        f"{index}. {rule}\n"
        for index, rule in enumerate(decision_rules, start=1)
    )
    instruction += (
        "\n"
        "Если данных мало — прямо напиши, что данных недостаточно, "
        "и не додумывай."
    )
    return instruction
