"""Тесты вспомогательной логики orchestrator для KB-поиска."""

from __future__ import annotations

from types import SimpleNamespace

from alla.knowledge.feedback_models import FeedbackRecord, FeedbackVote
from alla.knowledge.feedback_signature import build_feedback_cluster_context
from alla.knowledge.models import KBEntry, KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.onboarding import OnboardingMode
from alla.models.testops import FailedTestSummary
from alla.orchestrator import (
    _apply_exact_feedback_memory,
    _build_kb_query_text,
    _build_onboarding_state,
)


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
    """Если у representative нет лога, KB query строится как message + member log."""
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
    assert trace_len == 0
    assert log_len > 0
    assert "AssertionError: expected true" in query_text
    assert "at test.py:42" not in query_text
    assert "RootCauseException: boom" in query_text


def test_build_kb_query_text_falls_back_to_trace_when_log_missing() -> None:
    """При отсутствии application log KB query использует message + trace."""
    cluster = FailureCluster(
        cluster_id="c4",
        label="cluster",
        signature=ClusterSignature(),
        representative_test_id=301,
        member_test_ids=[301],
        member_count=1,
        example_message="fallback message",
        example_trace_snippet="fallback trace",
    )
    test_by_id = {
        301: _failed_test(
            301,
            status_message="AssertionError: expected false",
            status_trace="at test.py:99",
            log_snippet=None,
        ),
    }

    query_text, message_len, trace_len, log_len = _build_kb_query_text(
        cluster,
        test_by_id,
    )

    assert message_len > 0
    assert trace_len > 0
    assert log_len == 0
    assert "AssertionError: expected false" in query_text
    assert "at test.py:99" in query_text


def test_build_onboarding_state_guided_for_project_without_project_entries() -> None:
    """Guided onboarding включается, если у проекта нет project-scoped KB."""
    clustering = type(
        "Report",
        (),
        {
            "clusters": [
                FailureCluster(
                    cluster_id="c3",
                    label="cluster",
                    signature=ClusterSignature(),
                    member_test_ids=[1],
                    member_count=7,
                )
            ]
        },
    )()
    entries = [
        KBEntry(
            id="starter_pack_timeout",
            title="Starter pack timeout",
            description="",
            error_example="timeout",
            category=RootCauseCategory.ENV,
            resolution_steps=["check"],
            entry_id=1,
            project_id=None,
        )
    ]

    state = _build_onboarding_state(
        SimpleNamespace(kb_active=True),
        42,
        clustering,  # type: ignore[arg-type]
        kb_entries=entries,
    )

    assert state.mode == OnboardingMode.GUIDED
    assert state.needs_bootstrap is True
    assert state.project_kb_entries == 0
    assert state.starter_pack_available is True
    assert state.prioritized_cluster_ids == ["c3"]


def test_build_onboarding_state_marks_missing_kb_config() -> None:
    """При выключенной KB выдаётся setup-oriented режим."""
    state = _build_onboarding_state(
        SimpleNamespace(kb_active=False),
        42,
        None,
        kb_entries=[],
    )

    assert state.mode == OnboardingMode.KB_NOT_CONFIGURED
    assert state.needs_bootstrap is False


def test_feedback_signature_is_stable_for_large_logs_with_volatile_values() -> None:
    """Большой лог с разными timestamp/id даёт одинаковый exact issue signature."""
    cluster = FailureCluster(
        cluster_id="c-large",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message=(
            "Payment gateway timeout for order 12345 at 2026-02-10 12:00:00 "
            "from 10.20.30.40 in cluster one"
        ),
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message=cluster.example_message,
                log_snippet=(
                    "--- [файл: app.log] ---\n"
                    "2026-02-10 12:00:00 [ERROR] requestId=123e4567e89b12d3a456426614174000 "
                    "payment gateway timeout for order 12345\n"
                    "Caused by: java.net.ConnectException: Connection refused"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster.model_copy(
            update={
                "example_message": (
                    "Payment gateway timeout for order 67890 at 2026-02-11 13:45:00 "
                    "from 10.20.30.50 in cluster one"
                )
            }
        ),
        {
            1: _failed_test(
                1,
                status_message=(
                    "Payment gateway timeout for order 67890 at 2026-02-11 13:45:00 "
                    "from 10.20.30.50 in cluster one"
                ),
                log_snippet=(
                    "--- [файл: app.log] ---\n"
                    "2026-02-11 13:45:00 [ERROR] requestId=999e4567e89b12d3a456426614174999 "
                    "payment gateway timeout for order 67890\n"
                    "Caused by: java.net.ConnectException: Connection refused"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash == second.issue_signature.signature_hash


def test_feedback_signature_uses_anchor_for_short_message_when_log_differs() -> None:
    """Короткая ошибка не должна схлопываться, если причина в логе отличается."""
    cluster = FailureCluster(
        cluster_id="c-short",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway timeout while saving order",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet="2026-02-10 [ERROR] requestId=1\nCaused by: first root cause",
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet="2026-02-11 [ERROR] requestId=2\nCaused by: another detail",
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_uses_matched_error_line_for_short_message() -> None:
    """Short-case должен различать одинаковый summary с одной exception, но разными error lines."""
    cluster = FailureCluster(
        cluster_id="c-short-matched",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway timeout while saving order",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Order validation failed for region EU\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-11 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert second.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_is_case_insensitive_for_same_issue() -> None:
    """Одинаковая ошибка с другой капитализацией должна матчиться как один issue."""
    cluster = FailureCluster(
        cluster_id="c-case",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway Timeout While Saving Order",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway Timeout While Saving Order",
                log_snippet="2026-02-10 [ERROR] requestId=1\nCaused by: Root Cause Timeout",
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="gateway timeout while saving order",
                log_snippet="2026-02-11 [error] requestId=2\ncaused by: root cause timeout",
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.signature_hash == second.issue_signature.signature_hash


def test_feedback_signature_preserves_long_numeric_error_codes() -> None:
    """Разные длинные error-code не должны коллапсировать в один exact signature."""
    first_cluster = FailureCluster(
        cluster_id="c-code-1",
        label="DB error",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Database error code 10001",
    )
    second_cluster = first_cluster.model_copy(
        update={
            "cluster_id": "c-code-2",
            "example_message": "Database error code 10002",
        }
    )

    first = build_feedback_cluster_context(
        first_cluster,
        {
            1: _failed_test(
                1,
                status_message="Database error code 10001",
            ),
        },
    )
    second = build_feedback_cluster_context(
        second_cluster,
        {
            1: _failed_test(
                1,
                status_message="Database error code 10002",
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_exact"
    assert second.issue_signature.basis == "message_exact"
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_preserves_short_numeric_codes_even_with_same_anchor() -> None:
    """Короткий message с разными кодами не должен схлопываться даже при одинаковом anchor."""
    cluster = FailureCluster(
        cluster_id="c-anchor-code-1",
        label="Remote error",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Remote error code 10001",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Remote error code 10001",
                log_snippet="2026-02-10 [ERROR] requestId=1\nCaused by: BackendException: boom",
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster.model_copy(
            update={
                "cluster_id": "c-anchor-code-2",
                "example_message": "Remote error code 10002",
            }
        ),
        {
            1: _failed_test(
                1,
                status_message="Remote error code 10002",
                log_snippet="2026-02-11 [ERROR] requestId=2\nCaused by: BackendException: boom",
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert second.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_is_order_insensitive_for_anchor_lines() -> None:
    """Одинаковый набор anchor-строк в другом порядке должен давать тот же hash."""
    cluster = FailureCluster(
        cluster_id="c-order",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway timeout while saving order",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "Caused by: java.lang.IllegalStateException: Remote service failed\n"
                    "2026-02-11 [ERROR] Currency mismatch for payment gateway"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.signature_hash == second.issue_signature.signature_hash


def test_apply_exact_feedback_memory_pins_injected_entry_and_hides_disliked() -> None:
    """Exact memory может добавить liked entry и убрать disliked entry."""
    weak_entry = KBEntry(
        id="weak_match",
        title="Weak match",
        description="",
        error_example="weak",
        category=RootCauseCategory.SERVICE,
        resolution_steps=[],
        entry_id=10,
        project_id=42,
    )
    liked_entry = KBEntry(
        id="confirmed_match",
        title="Confirmed match",
        description="",
        error_example="confirmed",
        category=RootCauseCategory.SERVICE,
        resolution_steps=[],
        entry_id=20,
        project_id=42,
    )
    merged = _apply_exact_feedback_memory(
        [
            KBMatchResult(
                entry=weak_entry,
                score=0.33,
                matched_on=["Tier 3: TF-IDF similarity: 0.33"],
            )
        ],
        [
            FeedbackRecord(
                feedback_id=77,
                kb_entry_id=10,
                audit_text="legacy dislike",
                vote=FeedbackVote.DISLIKE,
                issue_signature_hash="a" * 64,
                issue_signature_version=2,
            ),
            FeedbackRecord(
                feedback_id=88,
                kb_entry_id=20,
                audit_text="confirmed like",
                vote=FeedbackVote.LIKE,
                issue_signature_hash="b" * 64,
                issue_signature_version=2,
            ),
        ],
        {
            10: weak_entry,
            20: liked_entry,
        },
        max_results=5,
    )

    assert len(merged) == 1
    assert merged[0].entry.entry_id == 20
    assert merged[0].match_origin == "feedback_exact"
    assert merged[0].score == 1.0
    assert merged[0].feedback_vote == "like"
    assert merged[0].feedback_id == 88
