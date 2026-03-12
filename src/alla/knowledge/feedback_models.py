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


class FeedbackRecord(BaseModel):
    """Запись голоса из БД для fuzzy matching."""

    kb_entry_id: int
    error_text: str
    vote: FeedbackVote


class FeedbackRequest(BaseModel):
    """Тело POST /api/v1/kb/feedback."""

    kb_entry_id: int = Field(
        description="Суррогатный PK записи KB (alla.kb_entry.entry_id)",
    )
    error_text: str = Field(
        min_length=1,
        description="Нормализованный текст ошибки (assertion + log, без trace)",
    )
    vote: FeedbackVote
    launch_id: int | None = Field(default=None, description="ID запуска (аудит)")
    cluster_id: str | None = Field(default=None, description="ID кластера (аудит)")


class FeedbackResponse(BaseModel):
    """Ответ POST /api/v1/kb/feedback."""

    kb_entry_id: int
    error_text_preview: str = Field(
        description="Первые 80 символов сохранённого error_text",
    )
    vote: FeedbackVote
    created: bool = Field(
        description="True — создан новый голос, False — обновлён существующий",
    )


# ------------------------------------------------------------------
# Resolve votes (batch fuzzy lookup)
# ------------------------------------------------------------------


class FeedbackResolveItem(BaseModel):
    """Элемент запроса на резолв голосов."""

    kb_entry_id: int
    error_text: str
    cluster_id: str = Field(
        default="",
        description="ID кластера — для disambiguation одной KB-записи в разных кластерах",
    )


class FeedbackResolveRequest(BaseModel):
    """Тело POST /api/v1/kb/feedback/resolve."""

    items: list[FeedbackResolveItem]


class FeedbackResolveVote(BaseModel):
    """Результат резолва для одного entry."""

    vote: FeedbackVote
    similarity: float


class FeedbackResolveResponse(BaseModel):
    """Ответ POST /api/v1/kb/feedback/resolve."""

    votes: dict[str, FeedbackResolveVote]


# ------------------------------------------------------------------
# Create KB entry
# ------------------------------------------------------------------


class CreateKBEntryRequest(BaseModel):
    """Тело POST /api/v1/kb/entries — создание записи KB из HTML-отчёта."""

    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9_]+$",
        description="Slug-идентификатор (строчные латинские, цифры, подчёркивания). Генерируется автоматически если не указан.",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Заголовок записи. Генерируется автоматически из error_example если не указан.",
    )
    description: str = Field(default="")
    error_example: str = Field(default="")
    category: RootCauseCategory = Field(default=RootCauseCategory.SERVICE)
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
