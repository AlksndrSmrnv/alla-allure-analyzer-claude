"""Pydantic-модели для системы обратной связи KB."""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from alla.knowledge.models import RootCauseCategory


class FeedbackVote(str, Enum):
    """Тип голоса тестировщика."""

    LIKE = "like"
    DISLIKE = "dislike"


class FeedbackIssueSignature(BaseModel):
    """Стабильная сигнатура ошибки для exact feedback memory."""

    DEFAULT_VERSION: ClassVar[int] = 1

    signature_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    version: int = Field(default=DEFAULT_VERSION, ge=1)
    basis: str = Field(min_length=1, max_length=64)


class FeedbackClusterContext(BaseModel):
    """Контекст кластера, который использует report UI для feedback."""

    audit_text: str = Field(min_length=1)
    issue_signature: FeedbackIssueSignature


# ------------------------------------------------------------------
# Feedback (like / dislike)
# ------------------------------------------------------------------


class FeedbackRecord(BaseModel):
    """Запись exact feedback из БД."""

    feedback_id: int | None = None
    kb_entry_id: int
    audit_text: str
    vote: FeedbackVote
    issue_signature_hash: str | None = None
    issue_signature_version: int | None = None
    issue_signature_payload: dict[str, Any] | None = None


class FeedbackRequest(BaseModel):
    """Тело POST /api/v1/kb/feedback."""

    kb_entry_id: int = Field(
        description="Суррогатный PK записи KB (alla.kb_entry.entry_id)",
    )
    audit_text: str = Field(
        min_length=1,
        description="Компактный audit-текст exact issue signature",
    )
    vote: FeedbackVote
    issue_signature_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    issue_signature_version: int = Field(default=FeedbackIssueSignature.DEFAULT_VERSION, ge=1)
    issue_signature_payload: dict[str, Any] | None = None
    launch_id: int | None = Field(default=None, description="ID запуска (аудит)")
    cluster_id: str | None = Field(default=None, description="ID кластера (аудит)")


class FeedbackResponse(BaseModel):
    """Ответ POST /api/v1/kb/feedback."""

    kb_entry_id: int
    audit_text_preview: str = Field(
        description="Первые 80 символов сохранённого audit_text",
    )
    vote: FeedbackVote
    created: bool = Field(
        description="True — создан новый голос, False — обновлён существующий",
    )
    feedback_id: int | None = Field(
        default=None,
        description="PK записи alla.kb_feedback (для отображения в UI)",
    )


# ------------------------------------------------------------------
# Resolve votes (batch exact lookup)
# ------------------------------------------------------------------


class FeedbackResolveItem(BaseModel):
    """Элемент запроса на резолв голосов."""

    kb_entry_id: int
    issue_signature_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    issue_signature_version: int = Field(default=FeedbackIssueSignature.DEFAULT_VERSION, ge=1)
    cluster_id: str = Field(
        default="",
        description="ID кластера — только для ключа ответа в UI",
    )


class FeedbackResolveRequest(BaseModel):
    """Тело POST /api/v1/kb/feedback/resolve."""

    items: list[FeedbackResolveItem]


class FeedbackResolveVote(BaseModel):
    """Результат резолва для одного entry."""

    vote: FeedbackVote
    feedback_id: int | None = Field(
        default=None,
        description="PK записи alla.kb_feedback (для отображения в UI)",
    )


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
