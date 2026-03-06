"""Тесты вспомогательной логики orchestrator для KB-поиска."""

from __future__ import annotations

from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.orchestrator import _build_kb_query_text


def _failed_test(
    test_result_id: int,
    *,
    status_message: str | None = None,
    status_trace: str | None = None,
    log_snippet: str | None = None,
) -> FailedTestSummary:
    return FailedTestSummary(
        test_result_id=test_result_id,
        name=f"test-{test_result_id}",
        status=Status.FAILED,
        status_message=status_message,
        status_trace=status_trace,
        log_snippet=log_snippet,
    )


def test_build_kb_query_text_uses_member_log_when_representative_has_none() -> None:
    """Если у representative нет лога, запрос включает лог другого теста кластера."""
    cluster = FailureCluster(
        cluster_id="c2",
        label="cluster",
        signature=ClusterSignature(),
        representative_test_id=201,
        member_test_ids=[201, 202],
        member_count=2,
        example_message="fallback message",
        example_trace_snippet="fallback trace",
    )
    test_by_id = {
        201: _failed_test(
            201,
            status_message="AssertionError: expected true",
            status_trace="at test.py:42",
            log_snippet=None,
        ),
        202: _failed_test(
            202,
            status_message="same issue",
            status_trace="at test.py:43",
            log_snippet="2026-02-10 [ERROR] RootCauseException: boom",
        ),
    }

    query_text, message_len, trace_len, log_len = _build_kb_query_text(
        cluster,
        test_by_id,
    )

    assert message_len > 0
    assert trace_len > 0
    assert log_len > 0
    assert "RootCauseException: boom" in query_text
