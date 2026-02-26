"""Pydantic-модели для системы обратной связи KB."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from alla.knowledge.models import RootCauseCategory


class FeedbackVote(str, Enum):
    """Тип голоса тестировщика."""

    LIKE = "like"
    DISLIKE = "dislike"


# ------------------------------------------------------------------
# Feedback (like / dislike)
# ------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """Тело POST /api/v1/kb/feedback."""

    kb_entry_id: int = Field(
        description="Суррогатный PK записи KB (alla.kb_entry.entry_id)",
    )
    error_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 hex нормализованного error_text",
    )
    vote: FeedbackVote
    launch_id: int | None = Field(default=None, description="ID запуска (аудит)")
    cluster_id: str | None = Field(default=None, description="ID кластера (аудит)")


class FeedbackResponse(BaseModel):
    """Ответ POST /api/v1/kb/feedback."""

    kb_entry_id: int
    error_fingerprint: str
    vote: FeedbackVote
    created: bool = Field(
        description="True — создан новый голос, False — обновлён существующий",
    )


# ------------------------------------------------------------------
# Create KB entry
# ------------------------------------------------------------------


class CreateKBEntryRequest(BaseModel):
    """Тело POST /api/v1/kb/entries — создание записи KB из HTML-отчёта."""

    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9_]+$",
        description="Slug-идентификатор (строчные латинские, цифры, подчёркивания)",
    )
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="")
    error_example: str = Field(min_length=1)
    category: RootCauseCategory
    resolution_steps: list[str] = Field(default_factory=list)
    project_id: int | None = Field(
        default=None,
        description="NULL → глобальная запись, N → запись для проекта N",
    )


class CreateKBEntryResponse(BaseModel):
    """Ответ POST /api/v1/kb/entries."""

    entry_id: int = Field(description="Суррогатный PK созданной записи")
    id: str = Field(description="Slug записи")
    title: str
    category: RootCauseCategory
    created: bool
