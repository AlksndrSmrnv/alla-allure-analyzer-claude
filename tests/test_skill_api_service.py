"""Тесты чистых трансформаций skill REST API (`skill_api_service`).

Эти функции раньше жили в скрипт-обёртках и обращались к PostgreSQL
напрямую; теперь они server-side и работают с уже загруженным SkillRun /
доменными моделями. Покрываем форму ответов, на которые завязаны
скрипты и REST-клиент.
"""

from __future__ import annotations

from alla.knowledge.models import KBEntry, KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla.models.common import TestStatus as StatusEnum
from alla.models.onboarding import OnboardingState
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services import skill_api_service as svc
from alla.services.skill_state_service import SkillRun, SKILL_RUN_SCHEMA_VERSION


def _triage(failed: list[FailedTestSummary] | None = None) -> TriageReport:
    return TriageReport(
        launch_id=123,
        launch_name="Run",
        project_id=1,
        total_results=10,
        passed_count=7,
        failed_count=3,
        broken_count=0,
        skipped_count=0,
        unknown_count=0,
        muted_failure_count=0,
        failed_tests=failed or [],
    )


def _cluster(cluster_id: str = "c-1", member_ids: list[int] | None = None) -> FailureCluster:
    members = member_ids or [1]
    return FailureCluster(
        cluster_id=cluster_id,
        label="ConnectionTimeout",
        signature=ClusterSignature(),
        member_test_ids=members,
        member_count=len(members),
        representative_test_id=members[0],
        example_message="Connection timed out after 30s",
        example_step_path="Login → Submit",
    )


def _clustering(clusters: list[FailureCluster]) -> ClusteringReport:
    return ClusteringReport(
        launch_id=123,
        total_failures=sum(c.member_count for c in clusters),
        cluster_count=len(clusters),
        unclustered_count=0,
        clusters=clusters,
    )


def _kb_match(title: str = "Network flake", score: float = 0.87) -> KBMatchResult:
    return KBMatchResult(
        entry=KBEntry(
            id="net_flake_abc12345",
            title=title,
            description="desc",
            error_example="socket.timeout",
            step_path=None,
            category=RootCauseCategory.SERVICE,
            resolution_steps=["retry"],
            entry_id=5,
        ),
        score=score,
        matched_on=["Tier 2: line match"],
        match_origin="kb",
    )


def _failed_test(test_result_id: int = 1) -> FailedTestSummary:
    return FailedTestSummary(
        test_result_id=test_result_id,
        test_case_id=100 + test_result_id,
        name=f"test_{test_result_id}",
        status=StatusEnum.FAILED,
        status_message="boom",
        status_trace="Traceback ...",
        log_snippet="ERROR connection timed out",
        failed_step_path="Login → Submit",
    )


def _skill_run(**overrides) -> SkillRun:
    defaults = dict(
        run_id=42,
        schema_version=SKILL_RUN_SCHEMA_VERSION,
        status="clustered",
        launch_id=123,
        project_id=1,
        launch_name="Run",
        triage_report=_triage([_failed_test(1)]),
        clustering_report=_clustering([_cluster(member_ids=[1])]),
        kb_results={"c-1": [_kb_match()]},
        onboarding=OnboardingState(),
    )
    defaults.update(overrides)
    return SkillRun(**defaults)


# --- build_run_summary --------------------------------------------------


def test_build_run_summary_shape_and_top_kb_match() -> None:
    report = _triage([_failed_test(1)])
    clustering = _clustering([_cluster()])
    summary = svc.build_run_summary(42, report, clustering, {"c-1": [_kb_match()]})

    assert summary["ok"] is True
    assert summary["run_id"] == 42
    assert summary["launch"] == {"id": 123, "name": "Run", "project_id": 1}
    assert summary["cluster_count"] == 1
    cluster_view = summary["clusters"][0]
    assert cluster_view["cluster_id"] == "c-1"
    assert cluster_view["kb_match_count"] == 1
    assert cluster_view["top_kb_match"]["tier"] == "Tier 2"
    assert cluster_view["top_kb_match"]["entry_id"] == 5


def test_build_run_summary_without_clustering() -> None:
    summary = svc.build_run_summary(7, _triage(), None, {})
    assert summary["cluster_count"] == 0
    assert summary["clusters"] == []


# --- build_cluster_context ----------------------------------------------


def test_build_cluster_context_returns_prompt_and_context() -> None:
    ctx = svc.build_cluster_context(_skill_run(), "c-1")
    assert ctx is not None
    assert ctx["cluster_id"] == "c-1"
    assert ctx["system_prompt"]
    assert ctx["user_prompt"]
    assert ctx["context"]["representative"]["test_result_id"] == 1
    assert ctx["context"]["kb_matches"][0]["entry_id"] == 5


def test_build_cluster_context_missing_cluster_returns_none() -> None:
    assert svc.build_cluster_context(_skill_run(), "nope") is None


def test_build_cluster_context_no_clustering_returns_none() -> None:
    assert svc.build_cluster_context(_skill_run(clustering_report=None), "c-1") is None


# --- build_summary_context ----------------------------------------------


def test_build_summary_context_shape() -> None:
    ctx = svc.build_summary_context(_skill_run())
    assert ctx is not None
    assert ctx["system_prompt"]
    assert ctx["context"]["cluster_count"] == 1
    assert ctx["context"]["top_clusters"][0]["cluster_id"] == "c-1"


def test_build_summary_context_no_clustering_returns_none() -> None:
    assert svc.build_summary_context(_skill_run(clustering_report=None)) is None


# --- serialize_run ------------------------------------------------------


def test_serialize_run_roundtrips_domain_models() -> None:
    run = _skill_run(
        agent_analysis={"schema_version": 1},
        report_url="http://x/reports/r.html",
    )
    data = svc.serialize_run(run)

    assert data["run_id"] == 42
    assert data["launch_id"] == 123
    assert data["agent_analysis"] == {"schema_version": 1}
    assert data["report_url"] == "http://x/reports/r.html"

    # Доменные модели должны восстанавливаться обратно.
    TriageReport.model_validate(data["triage_report"])
    ClusteringReport.model_validate(data["clustering_report"])
    assert data["kb_results"]["c-1"][0]["entry"]["entry_id"] == 5


# --- interactive_disabled_reasons --------------------------------------


def test_interactive_reasons_empty_when_configured() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(kb_active=True, feedback_server_url="http://x")
    assert svc.interactive_disabled_reasons(settings) == []


def test_interactive_reasons_flags_missing_url() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(kb_active=True, feedback_server_url="")
    assert svc.interactive_disabled_reasons(settings) == ["feedback_server_url_empty"]
