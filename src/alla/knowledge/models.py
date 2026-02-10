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
    error_pattern: str = Field(
        description="Подстрока для поиска в тексте ошибки (message + trace)"
    )
    category: RootCauseCategory = Field(
        description="Категория причины: test / service / env / data"
    )
    resolution_steps: list[str] = Field(
        default_factory=list, description="Шаги по устранению"
    )


class KBMatchResult(BaseModel):
    """Результат сопоставления ошибки с записью KB."""

    entry: KBEntry
    score: float = Field(
        ge=0.0, le=1.0,
        description="Степень совпадения (0.0 — нет, 1.0 — полное)",
    )
