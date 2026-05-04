"""Адаптер агентского анализа в формат, понятный остальной системе.

В skill-режиме анализ кластеров и итоговый summary прогона выполняет
сам агент CLI (Claude / qwen / codex). Скрипты скилла принимают от
агента JSON по схеме, описанной в
``alla-skill/references/analysis_schema.md``, и через этот адаптер
конвертируют его в существующие модели:

* :class:`alla.models.llm.LLMAnalysisResult`
* :class:`alla.models.llm.LLMLaunchSummary`

Это позволяет :func:`alla.report.html_report.generate_html_report` и
:func:`alla.services.comment_push_service.push_comments` работать без
правок: для них агентский анализ выглядит как любой другой LLM-результат.
"""

from __future__ import annotations

from typing import Any

from alla.models.clustering import ClusteringReport
from alla.models.llm import (
    LLMAnalysisResult,
    LLMClusterAnalysis,
    LLMLaunchSummary,
    TokenUsage,
)

__all__ = [
    "AGENT_ANALYSIS_SCHEMA_VERSION",
    "AGENT_CATEGORIES",
    "AGENT_CONFIDENCE_LEVELS",
    "agent_to_llm_result",
    "agent_to_launch_summary",
    "validate_agent_payload",
    "AgentAnalysisError",
]


AGENT_ANALYSIS_SCHEMA_VERSION = 1

AGENT_CATEGORIES: frozenset[str] = frozenset(
    {"test", "service", "env", "data", "unanalyzed"}
)
AGENT_CONFIDENCE_LEVELS: frozenset[str] = frozenset(
    {"high", "medium", "low"}
)

_MAX_ANALYSIS_TEXT_CHARS = 8000


class AgentAnalysisError(ValueError):
    """Семантическая ошибка валидации агентского payload'а."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_agent_payload(
    payload: Any,
    *,
    expected_cluster_ids: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Проверить, что ``payload`` соответствует схеме агентского анализа.

    Возвращает ``(missing_cluster_ids, extra_cluster_ids)`` — для
    отображения скриптом ``submit_analysis``. ``missing_cluster_ids`` —
    кластеры, для которых агент не прислал анализ; они не блокируют
    запись (warning, не ошибка). ``extra_cluster_ids`` — лишние
    идентификаторы, которых нет в clustering_report.

    Бросает :class:`AgentAnalysisError` при структурных проблемах:
    несовпадение ``schema_version``, отсутствие обязательных полей,
    запрещённые значения категории/confidence, превышение лимитов длины.
    """
    if not isinstance(payload, dict):
        raise AgentAnalysisError("Payload должен быть JSON-объектом")

    schema_version = payload.get("schema_version")
    if schema_version != AGENT_ANALYSIS_SCHEMA_VERSION:
        raise AgentAnalysisError(
            f"Unsupported schema_version={schema_version!r}, "
            f"expected {AGENT_ANALYSIS_SCHEMA_VERSION}"
        )

    launch_summary = payload.get("launch_summary")
    if not isinstance(launch_summary, dict):
        raise AgentAnalysisError("launch_summary должен быть объектом")
    summary_text = launch_summary.get("summary_text")
    if not isinstance(summary_text, str) or not summary_text.strip():
        raise AgentAnalysisError("launch_summary.summary_text не может быть пустым")

    clusters = payload.get("clusters")
    if not isinstance(clusters, dict):
        raise AgentAnalysisError("clusters должен быть объектом")

    for cluster_id, cluster_payload in clusters.items():
        if not isinstance(cluster_payload, dict):
            raise AgentAnalysisError(
                f"clusters.{cluster_id!r} должен быть объектом"
            )
        category = cluster_payload.get("category")
        if category not in AGENT_CATEGORIES:
            raise AgentAnalysisError(
                f"clusters.{cluster_id!r}.category={category!r}: "
                f"допустимые значения {sorted(AGENT_CATEGORIES)}"
            )
        confidence = cluster_payload.get("confidence")
        if confidence not in AGENT_CONFIDENCE_LEVELS:
            raise AgentAnalysisError(
                f"clusters.{cluster_id!r}.confidence={confidence!r}: "
                f"допустимые значения {sorted(AGENT_CONFIDENCE_LEVELS)}"
            )
        analysis_text = cluster_payload.get("analysis_text")
        if not isinstance(analysis_text, str) or not analysis_text.strip():
            raise AgentAnalysisError(
                f"clusters.{cluster_id!r}.analysis_text не может быть пустым"
            )
        if len(analysis_text) > _MAX_ANALYSIS_TEXT_CHARS:
            raise AgentAnalysisError(
                f"clusters.{cluster_id!r}.analysis_text > "
                f"{_MAX_ANALYSIS_TEXT_CHARS} chars"
            )

    if expected_cluster_ids is None:
        return [], []

    expected_set = set(expected_cluster_ids)
    received_set = set(clusters.keys())
    missing = sorted(expected_set - received_set)
    extra = sorted(received_set - expected_set)
    return missing, extra


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def agent_to_llm_result(
    agent_analysis: dict[str, Any],
    clustering_report: ClusteringReport,
) -> LLMAnalysisResult:
    """Сконвертировать агентский анализ в :class:`LLMAnalysisResult`.

    Категория ``unanalyzed`` (для tail в режиме >30 кластеров) отдаётся
    в ``cluster_analyses`` с ``error="…"`` и пустым ``analysis_text`` —
    это исключает их из push'а в TestOps (push постит только не-пустой
    ``analysis_text``).
    """
    clusters_payload: dict[str, Any] = agent_analysis.get("clusters", {})
    cluster_analyses: dict[str, LLMClusterAnalysis] = {}
    analyzed = 0
    failed = 0
    skipped = 0

    for cluster in clustering_report.clusters:
        cluster_id = cluster.cluster_id
        item = clusters_payload.get(cluster_id)
        if item is None:
            failed += 1
            cluster_analyses[cluster_id] = LLMClusterAnalysis(
                cluster_id=cluster_id,
                error="Кластер не проанализирован агентом",
            )
            continue

        category = item.get("category")
        if category == "unanalyzed":
            skipped += 1
            cluster_analyses[cluster_id] = LLMClusterAnalysis(
                cluster_id=cluster_id,
                error=item.get("analysis_text") or "tail (не проанализирован)",
            )
            continue

        text = _compose_cluster_text(item)
        cluster_analyses[cluster_id] = LLMClusterAnalysis(
            cluster_id=cluster_id,
            analysis_text=text,
        )
        analyzed += 1

    return LLMAnalysisResult(
        total_clusters=len(clustering_report.clusters),
        analyzed_count=analyzed,
        failed_count=failed,
        skipped_count=skipped,
        cluster_analyses=cluster_analyses,
        token_usage=TokenUsage(),
    )


def agent_to_launch_summary(agent_analysis: dict[str, Any]) -> LLMLaunchSummary:
    """Сконвертировать агентский launch_summary в :class:`LLMLaunchSummary`.

    ``summary_text`` агента — канонический текст, который рендерится в
    HTML-отчёте. Структурные поля ``key_findings`` / ``priority_actions``
    / ``unanalyzed_tail`` остаются в схеме для совместимости (см.
    :func:`validate_agent_payload`), но НЕ дописываются к
    ``summary_text``: серверный путь через GigaChat ничего не дописывает,
    и адаптер обязан давать тот же результат, чтобы skill-режим строил
    отчёт байт-в-байт идентичный server-side.

    Если агенту нужны эти блоки в HTML — он включает их сам в
    ``summary_text`` по серверному launch summary промпту.
    """
    summary_payload = agent_analysis.get("launch_summary") or {}
    summary_text = (summary_payload.get("summary_text") or "").rstrip()
    return LLMLaunchSummary(
        summary_text=summary_text,
        token_usage=TokenUsage(),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _compose_cluster_text(item: dict[str, Any]) -> str:
    """Канонический ``analysis_text`` для :class:`LLMClusterAnalysis`.

    Серверный путь (GigaChat) кладёт в ``analysis_text`` ровно то, что
    вернула модель по cluster-analysis промпту — там уже есть блок
    «КАК ИСПРАВИТЬ» с конкретными шагами. Чтобы skill-режим давал тот же
    HTML-отчёт и тот же user_prompt в launch summary
    (см. :func:`alla.services.prompt_builder_service.build_launch_summary_prompt`),
    адаптер ничего не дописывает: поле ``recommendations`` остаётся в
    схеме для обратной совместимости, но игнорируется — фикс-шаги должны
    быть в ``analysis_text``.
    """
    return (item.get("analysis_text") or "").rstrip()
