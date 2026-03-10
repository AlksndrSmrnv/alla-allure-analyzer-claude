"""Модели данных для onboarding состояния проекта."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class OnboardingScope(str, Enum):
    """Область действия onboarding."""

    PROJECT = "project"


class OnboardingMode(str, Enum):
    """Режим onboarding в отчёте."""

    GUIDED = "guided"
    NORMAL = "normal"
    KB_NOT_CONFIGURED = "kb_not_configured"


class OnboardingState(BaseModel):
    """Состояние project onboarding для отчёта и JSON API."""

    scope: OnboardingScope = Field(default=OnboardingScope.PROJECT)
    mode: OnboardingMode = Field(default=OnboardingMode.NORMAL)
    needs_bootstrap: bool = False
    project_kb_entries: int = 0
    prioritized_cluster_ids: list[str] = Field(default_factory=list)
    starter_pack_available: bool = False
