"""Общие перечисления и обобщённые модели."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TestStatus(str, Enum):
    """Статусы результатов тестов Allure TestOps."""

    PASSED = "passed"
    FAILED = "failed"
    BROKEN = "broken"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"

    __str__ = str.__str__

    @classmethod
    def failure_statuses(cls) -> set[TestStatus]:
        """Статусы, считающиеся падениями для целей триажа."""
        return {cls.FAILED, cls.BROKEN}


@dataclass(frozen=True)
class PushResult:
    """Результат операции push (KB/LLM) рекомендаций в TestOps."""

    total_tests: int
    updated_count: int
    failed_count: int
    skipped_count: int


class PageResponse(BaseModel, Generic[T]):
    """Обобщённый пагинированный ответ от Allure TestOps API."""

    model_config = ConfigDict(populate_by_name=True)

    content: list[T]
    total_elements: int = Field(alias="totalElements")
    total_pages: int = Field(alias="totalPages")
    size: int
    number: int  # Номер текущей страницы (с 0)
