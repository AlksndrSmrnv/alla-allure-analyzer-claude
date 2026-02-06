"""Pydantic-модели для базы знаний (KB) об известных ошибках."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RootCauseCategory(str, Enum):
    """Категория корневой причины ошибки."""

    TEST = "test"
    APP = "app"
    ENV = "env"
    DATA = "data"


class SeverityHint(str, Enum):
    """Рекомендуемая срочность реагирования."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class KBEntryMatchCriteria(BaseModel):
    """Критерии сопоставления записи KB с реальной ошибкой.

    При миграции на RAG:
    - keywords, message_patterns, trace_patterns, exception_types → текст для embedding
    - categories → metadata filter в vector DB
    """

    keywords: list[str] = Field(default_factory=list)
    message_patterns: list[str] = Field(default_factory=list)
    trace_patterns: list[str] = Field(default_factory=list)
    exception_types: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class KBEntry(BaseModel):
    """Запись базы знаний — известная ошибка с описанием и рекомендациями."""

    id: str = Field(description="Уникальный идентификатор записи (slug)")
    title: str = Field(description="Человекочитаемое название проблемы")
    description: str = Field(default="", description="Подробное описание проблемы")
    root_cause: RootCauseCategory = Field(description="Категория корневой причины")
    severity: SeverityHint = Field(
        default=SeverityHint.MEDIUM, description="Рекомендуемая срочность"
    )
    match_criteria: KBEntryMatchCriteria = Field(
        default_factory=KBEntryMatchCriteria,
        description="Критерии сопоставления с ошибками",
    )
    resolution_steps: list[str] = Field(
        default_factory=list, description="Шаги по устранению"
    )
    related_links: list[str] = Field(
        default_factory=list, description="Ссылки на документацию/тикеты"
    )
    tags: list[str] = Field(
        default_factory=list, description="Теги для дополнительной фильтрации"
    )


class KBMatchResult(BaseModel):
    """Результат сопоставления ошибки с записью KB."""

    entry: KBEntry
    score: float = Field(
        ge=0.0, le=1.0,
        description="Степень совпадения (0.0 — нет, 1.0 — полное)",
    )
    matched_on: list[str] = Field(
        default_factory=list,
        description="Какие критерии совпали (для объяснимости)",
    )
