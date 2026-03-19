"""Pydantic-модели для базы знаний (KB) об известных ошибках."""

from __future__ import annotations

from enum import Enum
from typing import Literal

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
    step_path: str | None = Field(
        default=None,
        description=(
            "Опциональный breadcrumb шага теста. Если задан, запись становится "
            "step-aware: шаг участвует в KB-binding и exact feedback."
        ),
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
    match_origin: Literal["kb", "feedback_exact"] = Field(
        default="kb",
        description="Откуда появился результат: text-match KB или exact feedback memory",
    )
    feedback_vote: str | None = Field(
        default=None,
        description=(
            "Pre-resolved голос для exact-memory совпадения: 'like', 'dislike' или None."
        ),
    )
    feedback_id: int | None = Field(
        default=None,
        description="PK записи alla.kb_feedback для exact-memory совпадения.",
    )
