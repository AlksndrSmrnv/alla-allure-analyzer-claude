"""Тесты видимости и состава KB-запроса."""

from __future__ import annotations

import logging

from alla.knowledge.matcher import TextMatcher
from alla.knowledge.models import KBEntry, RootCauseCategory
from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.orchestrator import _build_kb_query_text


def _failed_summary(
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


def test_build_kb_query_uses_member_log_when_representative_has_no_log() -> None:
    """Если у representative нет лога, берём лог другого теста кластера."""
    cluster = FailureCluster(
        cluster_id="cluster-1",
        label="Test cluster",
        signature=ClusterSignature(),
        member_test_ids=[101, 102],
        member_count=2,
        representative_test_id=101,
        example_message="fallback message",
        example_trace_snippet="fallback trace",
    )
    test_by_id = {
        101: _failed_summary(
            101,
            status_message="AssertionError: expected 200",
            status_trace="at test.py:42",
            log_snippet=None,
        ),
        102: _failed_summary(
            102,
            status_message="same failure",
            status_trace="at test.py:43",
            log_snippet="2026-02-10 12:00:00 [ERROR] RootCauseException: boom",
        ),
    }

    query_text, message_len, trace_len, log_chars, log_test_ids = _build_kb_query_text(
        cluster,
        test_by_id,
    )

    assert message_len > 0
    assert trace_len > 0
    assert log_chars > 0
    assert log_test_ids == [102]
    assert "RootCauseException: boom" in query_text


def test_matcher_logs_head_and_tail_for_no_matches(caplog) -> None:
    """При отсутствии совпадений лог содержит и начало, и конец запроса."""
    matcher = TextMatcher()
    entries = [
        KBEntry.model_validate(
            {
                "id": "dns-failure",
                "title": "DNS failure",
                "description": "DNS resolution issue",
                "error_pattern": "UnknownHostException",
                "category": RootCauseCategory.ENV.value,
                "resolution_steps": [],
            }
        ),
    ]
    error_text = "ALLURE_MESSAGE " + ("x" * 320) + " LOG_ROOT_CAUSE boom"

    with caplog.at_level(logging.DEBUG):
        matcher.match(error_text, entries, query_label="cluster-abc")

    logs = caplog.text
    assert "KB: нет совпадений [cluster-abc]" in logs
    assert "ALLURE_MESSAGE" in logs
    assert "LOG_ROOT_CAUSE boom" in logs
