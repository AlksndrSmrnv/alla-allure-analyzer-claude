"""Pydantic-модели для базы знаний (KB) об известных ошибках."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RootCauseCategory(str, Enum):
    """Категория корневой причины ошибки."""

    TEST = "test"
    SERVICE = "service"
    ENV = "env"
    DATA = "data"


class KBEntry(BaseModel):
    """Запись базы знаний — известная ошибка с описанием и рекомендациями."""

    id: str = Field(description="Уникальный идентификатор записи (slug)")
    title: str = Field(description="Человекочитаемое название проблемы")
    description: str = Field(default="", description="Подробное описание проблемы")
    error_example: str = Field(
        description="Пример ошибки из лога — используется для нечёткого сопоставления (TF-IDF)"
    )
    category: RootCauseCategory = Field(
        description="Категория причины: test / service / env / data"
    )
    resolution_steps: list[str] = Field(
        default_factory=list, description="Шаги по устранению"
    )
    entry_id: int | None = Field(
        default=None,
        description=(
            "Суррогатный PK из PostgreSQL (alla.kb_entry.entry_id). "
            "None для YAML-бэкенда. Используется для feedback (like/dislike)."
        ),
    )
    project_id: int | None = Field(
        default=None,
        description=(
            "NULL = глобальная/starter-pack запись; N = запись конкретного "
            "проекта Allure TestOps."
        ),
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
        description="Объяснение совпадения (что именно совпало)",
    )
    feedback_vote: str | None = Field(
        default=None,
        description=(
            "Pre-resolved голос для данного совпадения: 'like', 'dislike' или None. "
            "Вычисляется при KB-matching через fuzzy similarity."
        ),
    )
    feedback_similarity: float | None = Field(
        default=None,
        description="Cosine similarity между текущей ошибкой и сохранённым feedback (0–1).",
    )
