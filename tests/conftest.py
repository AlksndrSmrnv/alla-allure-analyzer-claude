"""Общие фабрики и фикстуры для тестов alla."""

from __future__ import annotations

from alla.knowledge.models import (
    KBEntry,
    RootCauseCategory,
)
from alla.models.testops import ExecutionStep


def make_kb_entry(**overrides) -> KBEntry:
    """Фабрика KBEntry с разумными дефолтами."""
    defaults = {
        "id": "test_entry",
        "title": "Test Entry",
        "description": "A test KB entry",
        "error_example": "test error",
        "category": RootCauseCategory.SERVICE,
        "resolution_steps": ["Fix the issue"],
    }
    defaults.update(overrides)
    return KBEntry.model_validate(defaults)


def make_execution_step(**overrides) -> ExecutionStep:
    """Фабрика ExecutionStep с дефолтами."""
    defaults: dict = {}
    defaults.update(overrides)
    return ExecutionStep.model_validate(defaults)
