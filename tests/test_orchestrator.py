"""Тесты вспомогательной логики orchestrator для KB-поиска."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from alla.config import Settings
from alla.knowledge.feedback_models import FeedbackRecord, FeedbackVote
from alla.knowledge.feedback_signature import build_feedback_cluster_context
from alla.knowledge.models import KBEntry, KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla.models.llm import LLMAnalysisResult, LLMLaunchSummary
from alla.models.common import TestStatus as Status
from alla.models.onboarding import OnboardingMode
from alla.models.testops import FailedTestSummary, TriageReport
from alla.orchestrator import _KBStageResult
from alla.orchestrator import (
    _apply_exact_feedback_memory,
    _build_kb_query_text,
    _build_onboarding_state,
    _run_llm_stage,
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


def _llm_settings() -> Settings:
    return Settings(
        endpoint="https://allure.example.com",
        token="tok",
        gigachat_base_url="https://gigachat.example.com/api/v1",
        gigachat_cert_b64="Y2VydA==",
        gigachat_key_b64="a2V5",
    )


def _single_cluster_report() -> ClusteringReport:
    return ClusteringReport(
        launch_id=1,
        total_failures=1,
        cluster_count=1,
        clusters=[
            FailureCluster(
                cluster_id="c-1",
                label="cluster",
                signature=ClusterSignature(),
                member_test_ids=[1],
                member_count=1,
                example_message="boom",
            )
        ],
    )


def _single_triage_report() -> TriageReport:
    return TriageReport(
        launch_id=1,
        total_results=3,
        failed_count=1,
        failed_tests=[],
    )


@pytest.mark.asyncio
async def test_run_llm_stage_returns_none_when_cert_resolution_fails(monkeypatch) -> None:
    """Ошибка resolve_cert_files не должна валить весь pipeline."""
    settings = _llm_settings()

    def fail_resolve_cert_files(self: Settings) -> tuple[str, str]:
        raise ValueError("bad cert payload")

    monkeypatch.setattr(Settings, "resolve_cert_files", fail_resolve_cert_files)

    result, summary = await _run_llm_stage(
        _single_triage_report(),
        _single_cluster_report(),
        settings,
        kb_stage=_KBStageResult(),
    )

    assert result is None
    assert summary is None


@pytest.mark.asyncio
async def test_run_llm_stage_cleans_up_temp_files_when_client_init_fails(
    monkeypatch,
    tmp_path,
) -> None:
    """Temp cert/key файлы удаляются даже если GigaChatClient не инициализировался."""
    settings = _llm_settings()
    cert_path = tmp_path / "client-cert.pem"
    key_path = tmp_path / "client-key.pem"
    cert_path.write_text("cert", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")

    def fake_resolve_cert_files(self: Settings) -> tuple[str, str]:
        return str(cert_path), str(key_path)

    monkeypatch.setattr(Settings, "resolve_cert_files", fake_resolve_cert_files)

    from alla.clients import gigachat_client

    def fail_init(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("sdk init failed")

    monkeypatch.setattr(gigachat_client.GigaChatClient, "__init__", fail_init)

    result, summary = await _run_llm_stage(
        _single_triage_report(),
        _single_cluster_report(),
        settings,
        kb_stage=_KBStageResult(),
    )

    assert result is None
    assert summary is None
    assert not Path(cert_path).exists()
    assert not Path(key_path).exists()


@pytest.mark.asyncio
async def test_run_llm_stage_preserves_cluster_results_when_summary_generation_fails(
    monkeypatch,
    tmp_path,
) -> None:
    """Ошибка launch-summary не должна стирать уже успешный cluster analysis."""
    settings = _llm_settings()
    cert_path = tmp_path / "client-cert.pem"
    key_path = tmp_path / "client-key.pem"
    cert_path.write_text("cert", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    expected_result = LLMAnalysisResult(
        total_clusters=1,
        analyzed_count=1,
        failed_count=0,
        skipped_count=0,
    )

    def fake_resolve_cert_files(self: Settings) -> tuple[str, str]:
        return str(cert_path), str(key_path)

    monkeypatch.setattr(Settings, "resolve_cert_files", fake_resolve_cert_files)

    from alla.clients import gigachat_client
    from alla.services import llm_service

    class _FakeGigaChatClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def close(self) -> None:
            pass

    class _FakeLLMService:
        def __init__(self, client: object, **kwargs: object) -> None:
            self._client = client

        async def analyze_clusters(self, *args: object, **kwargs: object) -> LLMAnalysisResult:
            return expected_result

        async def generate_launch_summary(
            self,
            *args: object,
            **kwargs: object,
        ) -> LLMLaunchSummary:
            raise RuntimeError("summary prompt failed")

    monkeypatch.setattr(gigachat_client, "GigaChatClient", _FakeGigaChatClient)
    monkeypatch.setattr(llm_service, "LLMService", _FakeLLMService)

    result, summary = await _run_llm_stage(
        _single_triage_report(),
        _single_cluster_report(),
        settings,
        kb_stage=_KBStageResult(),
    )

    assert result == expected_result
    assert summary == LLMLaunchSummary(summary_text="", error="summary prompt failed")
    assert not cert_path.exists()
    assert not key_path.exists()


@pytest.mark.asyncio
async def test_run_llm_stage_preserves_results_when_client_close_fails(
    monkeypatch,
    tmp_path,
) -> None:
    """Ошибка close() не должна стирать уже полученные LLM results."""
    settings = _llm_settings()
    cert_path = tmp_path / "client-cert.pem"
    key_path = tmp_path / "client-key.pem"
    cert_path.write_text("cert", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    expected_result = LLMAnalysisResult(
        total_clusters=1,
        analyzed_count=1,
        failed_count=0,
        skipped_count=0,
    )
    expected_summary = LLMLaunchSummary(summary_text="ok")

    def fake_resolve_cert_files(self: Settings) -> tuple[str, str]:
        return str(cert_path), str(key_path)

    monkeypatch.setattr(Settings, "resolve_cert_files", fake_resolve_cert_files)

    from alla.clients import gigachat_client
    from alla.services import llm_service

    class _FakeGigaChatClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def close(self) -> None:
            raise RuntimeError("close failed")

    class _FakeLLMService:
        def __init__(self, client: object, **kwargs: object) -> None:
            self._client = client

        async def analyze_clusters(self, *args: object, **kwargs: object) -> LLMAnalysisResult:
            return expected_result

        async def generate_launch_summary(
            self,
            *args: object,
            **kwargs: object,
        ) -> LLMLaunchSummary:
            return expected_summary

    monkeypatch.setattr(gigachat_client, "GigaChatClient", _FakeGigaChatClient)
    monkeypatch.setattr(llm_service, "LLMService", _FakeLLMService)

    result, summary = await _run_llm_stage(
        _single_triage_report(),
        _single_cluster_report(),
        settings,
        kb_stage=_KBStageResult(),
    )

    assert result == expected_result
    assert summary == expected_summary
    assert not cert_path.exists()
    assert not key_path.exists()


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


def test_feedback_signature_ignores_http_sections_in_log_anchor() -> None:
    """HTTP-метаданные не должны менять exact-feedback signature."""
    cluster = FailureCluster(
        cluster_id="c-http-anchor",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway timeout",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout",
                log_snippet=(
                    "--- [файл: app.log] ---\n"
                    "retry budget exhausted while saving order\n"
                    "\n"
                    "--- [HTTP: response.json] ---\n"
                    "HTTP статус: 503\n"
                    "error: Service unavailable"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout",
                log_snippet=(
                    "--- [файл: app.log] ---\n"
                    "retry budget exhausted while saving order\n"
                    "\n"
                    "--- [HTTP: response.json] ---\n"
                    "HTTP статус: 502\n"
                    "error: Upstream gateway timeout"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert second.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash == second.issue_signature.signature_hash


def test_feedback_signature_uses_matched_error_line_for_short_message() -> None:
    """Короткий случай должен различать одинаковый summary с разными error lines."""
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


def test_feedback_signature_is_stable_when_representative_log_falls_back_to_members() -> None:
    """Одинаковый набор member logs даёт тот же hash даже при других test_result_id."""
    first_cluster = FailureCluster(
        cluster_id="c-fallback-a",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=10,
        member_test_ids=[10, 11, 12],
        member_count=3,
        example_message="Gateway timeout while saving order",
    )
    second_cluster = FailureCluster(
        cluster_id="c-fallback-b",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=20,
        member_test_ids=[20, 21, 22],
        member_count=3,
        example_message="Gateway timeout while saving order",
    )

    first = build_feedback_cluster_context(
        first_cluster,
        {
            10: _failed_test(10, status_message="Gateway timeout while saving order"),
            11: _failed_test(
                11,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Region EU validation failed\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
            12: _failed_test(
                12,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        second_cluster,
        {
            20: _failed_test(20, status_message="Gateway timeout while saving order"),
            21: _failed_test(
                21,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
            22: _failed_test(
                22,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Region EU validation failed\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.basis == "message_log_anchor"
    assert second.issue_signature.basis == "message_log_anchor"
    assert first.issue_signature.signature_hash == second.issue_signature.signature_hash


def test_feedback_signature_fallback_ignores_richer_member_log_for_same_issue() -> None:
    """Более подробный member-log не должен менять fallback hash при той же core issue."""
    base_cluster = FailureCluster(
        cluster_id="c-fallback-rich-base",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=10,
        member_test_ids=[10, 11],
        member_count=2,
        example_message="Gateway timeout while saving order",
    )
    expanded_cluster = FailureCluster(
        cluster_id="c-fallback-rich-expanded",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=20,
        member_test_ids=[20, 21, 22],
        member_count=3,
        example_message="Gateway timeout while saving order",
    )

    base = build_feedback_cluster_context(
        base_cluster,
        {
            10: _failed_test(10, status_message="Gateway timeout while saving order"),
            11: _failed_test(
                11,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )
    expanded = build_feedback_cluster_context(
        expanded_cluster,
        {
            20: _failed_test(20, status_message="Gateway timeout while saving order"),
            21: _failed_test(
                21,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
            22: _failed_test(
                22,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed\n"
                    "java.lang.RuntimeException: upstream failed"
                ),
            ),
        },
    )

    assert base is not None
    assert expanded is not None
    assert base.issue_signature.signature_hash == expanded.issue_signature.signature_hash


def test_feedback_signature_does_not_drift_when_representative_log_stays_the_same() -> None:
    """Дополнительный member-log не должен менять hash, если representative log не менялся."""
    base_cluster = FailureCluster(
        cluster_id="c-representative-stable",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Gateway timeout while saving order",
    )
    expanded_cluster = base_cluster.model_copy(
        update={"member_test_ids": [1, 2], "member_count": 2}
    )

    base = build_feedback_cluster_context(
        base_cluster,
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
    expanded = build_feedback_cluster_context(
        expanded_cluster,
        {
            1: _failed_test(
                1,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Currency mismatch for payment gateway\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
            2: _failed_test(
                2,
                status_message="Gateway timeout while saving order",
                log_snippet=(
                    "2026-02-10 [ERROR] Region EU validation failed\n"
                    "Caused by: java.lang.IllegalStateException: Remote service failed"
                ),
            ),
        },
    )

    assert base is not None
    assert expanded is not None
    assert base.issue_signature.signature_hash == expanded.issue_signature.signature_hash


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


def test_feedback_signature_preserves_long_numeric_codes_in_log_anchor() -> None:
    """Soft-normalized log-anchor должен различать разные error-code."""
    cluster = FailureCluster(
        cluster_id="c-log-code",
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
                    "2026-02-10 [ERROR] Backend returned code 10001\n"
                    "Caused by: java.lang.RuntimeException: upstream failed"
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
                    "2026-02-10 [ERROR] Backend returned code 10002\n"
                    "Caused by: java.lang.RuntimeException: upstream failed"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_preserves_embedded_numeric_codes_in_log_anchor() -> None:
    """Встроенные codes вроде ORA-12541 и ORA-12514 не должны схлопываться."""
    cluster = FailureCluster(
        cluster_id="c-ora-code",
        label="Oracle error",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="Database connect failure",
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Database connect failure",
                log_snippet=(
                    "2026-02-10 [ERROR] Oracle returned ORA-12541 during connect\n"
                    "Caused by: java.sql.SQLException: connect failed"
                ),
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message="Database connect failure",
                log_snippet=(
                    "2026-02-10 [ERROR] Oracle returned ORA-12514 during connect\n"
                    "Caused by: java.sql.SQLException: connect failed"
                ),
            ),
        },
    )

    assert first is not None
    assert second is not None
    assert first.issue_signature.signature_hash != second.issue_signature.signature_hash


def test_feedback_signature_preserves_long_numeric_codes_in_anchored_message() -> None:
    """Длинный message с одинаковым anchor не должен схлопываться по soft-path."""
    first_message = (
        "Gateway timeout while saving order after backend returned code 10001 "
        "during multi-step reconciliation in payment pipeline with retry attempt "
        "still pending for downstream orchestration"
    )
    second_message = (
        "Gateway timeout while saving order after backend returned code 10002 "
        "during multi-step reconciliation in payment pipeline with retry attempt "
        "still pending for downstream orchestration"
    )
    cluster = FailureCluster(
        cluster_id="c-long-code",
        label="Gateway timeout",
        signature=ClusterSignature(),
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message=first_message,
    )
    shared_log = (
        "2026-02-10 [ERROR] gateway timeout while saving order\n"
        "Caused by: java.lang.RuntimeException: upstream failed"
    )
    first = build_feedback_cluster_context(
        cluster,
        {
            1: _failed_test(
                1,
                status_message=first_message,
                log_snippet=shared_log,
            ),
        },
    )
    second = build_feedback_cluster_context(
        cluster.model_copy(update={"example_message": second_message}),
        {
            1: _failed_test(
                1,
                status_message=second_message,
                log_snippet=shared_log,
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
                issue_signature_version=5,
            ),
            FeedbackRecord(
                feedback_id=88,
                kb_entry_id=20,
                audit_text="confirmed like",
                vote=FeedbackVote.LIKE,
                issue_signature_hash="b" * 64,
                issue_signature_version=5,
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
