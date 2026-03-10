"""Модели данных для результатов LLM-анализа кластеров ошибок."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel


class LLMClusterAnalysis(BaseModel):
    """Результат LLM-анализа одного кластера ошибок."""

    cluster_id: str
    analysis_text: str = ""
    error: str | None = None


@dataclass(frozen=True)
class LLMAnalysisResult:
    """Агрегированный результат LLM-анализа всех кластеров."""

    total_clusters: int
    analyzed_count: int
    failed_count: int
    skipped_count: int
    # Поле оставлено для обратной совместимости внешнего JSON-контракта.
    # После отказа от exact-KB bypass всегда равно 0.
    kb_bypass_count: int = 0
    cluster_analyses: dict[str, LLMClusterAnalysis] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMPushResult:
    """Результат записи LLM-рекомендаций в TestOps."""

    total_tests: int
    updated_count: int
    failed_count: int
    skipped_count: int


@dataclass(frozen=True)
class LLMLaunchSummary:
    """Итоговый LLM-отчёт по всему прогону тестов."""

    summary_text: str
    error: str | None = None
