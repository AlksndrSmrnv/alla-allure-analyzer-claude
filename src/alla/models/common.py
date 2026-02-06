"""Shared enums and generic models."""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TestStatus(str, Enum):
    """Allure TestOps test result statuses."""

    PASSED = "passed"
    FAILED = "failed"
    BROKEN = "broken"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"

    @classmethod
    def failure_statuses(cls) -> set[TestStatus]:
        """Statuses considered as failures for triage purposes."""
        return {cls.FAILED, cls.BROKEN}


class PageResponse(BaseModel, Generic[T]):
    """Generic paginated response from Allure TestOps API."""

    model_config = ConfigDict(populate_by_name=True)

    content: list[T]
    total_elements: int = Field(alias="totalElements")
    total_pages: int = Field(alias="totalPages")
    size: int
    number: int  # 0-based current page
