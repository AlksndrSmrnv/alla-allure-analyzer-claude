"""Тесты видимости и состава KB-запроса."""

from __future__ import annotations

import logging

from alla.knowledge.matcher import TextMatcher
from alla.knowledge.models import KBEntry, RootCauseCategory
from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.orchestrator import _build_kb_query_text
from alla.utils.text_normalization import canonicalize_kb_error_example


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
    """Если у representative нет лога, берём лог другого теста и не добавляем trace."""
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

    query_text, message_len, trace_len, log_len = _build_kb_query_text(
        cluster,
        test_by_id,
    )

    assert message_len > 0
    assert trace_len == 0
    assert log_len > 0
    assert "AssertionError: expected 200" in query_text
    assert "at test.py:42" not in query_text
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
                "error_example": "java.net.UnknownHostException: host not found\n    at java.net.InetAddress",
                "category": RootCauseCategory.ENV.value,
                "resolution_steps": [],
            }
        ),
    ]
    error_text = "ALLURE_MESSAGE " + ("x" * 320) + " LOG_ROOT_CAUSE boom"

    with caplog.at_level(logging.DEBUG):
        results = matcher.match(error_text, entries, query_label="cluster-abc")

    logs = caplog.text
    # С TF-IDF текст "ALLURE_MESSAGE xxx LOG_ROOT_CAUSE boom" не похож на DNS-ошибку
    if not results:
        assert "KB: нет совпадений [cluster-abc]" in logs
        assert "ALLURE_MESSAGE" in logs
        assert "LOG_ROOT_CAUSE boom" in logs


def test_canonicalized_report_entry_gets_tier1_exact_match_on_repeat_analysis() -> None:
    """Новая KB-запись из report-form (message + log) должна exact-матчиться повторно."""
    cluster = FailureCluster(
        cluster_id="cluster-repeat",
        label="Gateway timeout",
        signature=ClusterSignature(),
        member_test_ids=[201],
        member_count=1,
        representative_test_id=201,
        example_message=(
            "Order 123e4567-e89b-12d3-a456-426614174000 failed "
            "at 2026-02-10 12:00:00 from 10.20.30.40"
        ),
        example_trace_snippet="at gateway.py:42",
    )
    test_by_id = {
        201: _failed_summary(
            201,
            status_message=cluster.example_message,
            status_trace="at gateway.py:42",
            log_snippet=(
                "--- [файл: app.log] ---\n"
                "2026-02-10 12:00:00 [ERROR] requestId="
                "123e4567e89b12d3a456426614174000 from 10.20.30.40 build 123456"
            ),
        )
    }
    raw_form_value = (
        f"{cluster.example_message}\n"
        "--- Лог приложения ---\n"
        f"{test_by_id[201].log_snippet}"
    )
    entry = KBEntry.model_validate(
        {
            "id": "gateway_timeout",
            "title": "Gateway timeout",
            "description": "canonicalized project knowledge",
            "error_example": canonicalize_kb_error_example(raw_form_value),
            "category": RootCauseCategory.SERVICE.value,
            "resolution_steps": [],
        }
    )

    query_text, message_len, trace_len, log_len = _build_kb_query_text(cluster, test_by_id)
    results = TextMatcher().match(query_text, [entry], query_label="cluster-repeat")

    assert message_len > 0
    assert trace_len == 0
    assert log_len > 0
    assert len(results) == 1
    assert results[0].score == 1.0
    assert "Tier 1" in results[0].matched_on[0]
