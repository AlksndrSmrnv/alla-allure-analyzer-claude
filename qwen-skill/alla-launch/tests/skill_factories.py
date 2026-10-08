"""Фабрики моделей ядра ``alla_core`` для тестов (``test_core_*.py``, сигнатура).

Как и ``skill_fixtures``, это не ``conftest.py``: модули импортируют фабрики явно.
Импортировать после ``skill_fixtures`` — он добавляет ``scripts/`` в ``sys.path``.
"""

from __future__ import annotations

from alla_core.models.clustering import ClusterSignature, FailureCluster
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


def make_single_test_cluster(
    message: str = "",
    trace: str = "",
    log: str = "",
    test_id: int = 1,
) -> tuple[FailureCluster, dict[int, FailedTestSummary]]:
    """Кластер из одного падения и словарь тестов — аргументы ``cluster_signature``."""
    test = make_failed_test_summary(test_result_id=test_id, status_message=message or None,
                                    status_trace=trace or None, log_snippet=log or None)
    cluster = FailureCluster(
        cluster_id=f"c{test_id}", label="l", signature=ClusterSignature(),
        member_test_ids=[test_id], member_count=1, representative_test_id=test_id,
        example_message=message or None,
    )
    return cluster, {test_id: test}
