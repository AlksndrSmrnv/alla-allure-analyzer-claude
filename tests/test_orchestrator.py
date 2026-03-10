"""Тесты вспомогательной логики orchestrator для KB-поиска."""

from __future__ import annotations

from types import SimpleNamespace

from alla.knowledge.models import KBEntry, RootCauseCategory
from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.onboarding import OnboardingMode
from alla.models.testops import FailedTestSummary
from alla.orchestrator import _build_kb_query_text, _build_onboarding_state


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
