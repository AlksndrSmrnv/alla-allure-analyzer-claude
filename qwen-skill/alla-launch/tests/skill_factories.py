"""Фабрики моделей ядра ``alla_core`` для тестов ядра (``test_core_*.py``).

Как и ``skill_fixtures``, это не ``conftest.py``: модули импортируют фабрики явно.
Импортировать после ``skill_fixtures`` — он добавляет ``scripts/`` в ``sys.path``.
"""

from __future__ import annotations

from alla_core.models.common import TestStatus as StatusEnum
from alla_core.models.testops import ExecutionStep, FailedTestSummary


def make_execution_step(**overrides) -> ExecutionStep:
    """Фабрика ExecutionStep с дефолтами."""
    defaults: dict = {}
    defaults.update(overrides)
    return ExecutionStep.model_validate(defaults)


def make_failed_test_summary(**overrides) -> FailedTestSummary:
    """Фабрика FailedTestSummary с разумными дефолтами."""
    defaults: dict = {
        "test_result_id": 1,
        "name": "test_example",
        "status": StatusEnum.FAILED,
    }
    defaults.update(overrides)
    return FailedTestSummary.model_validate(defaults)
