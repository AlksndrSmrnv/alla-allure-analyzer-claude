"""Behavioral tests for HTML report generation."""

from __future__ import annotations

import html as _html
import re

from alla.knowledge.feedback_models import FeedbackClusterContext, FeedbackIssueSignature
from alla.models.onboarding import OnboardingMode, OnboardingState
from alla.orchestrator import AnalysisResult
from alla.report.html_report import generate_html_report

from conftest import (
    make_clustering_report,
    make_failed_test_summary,
    make_failure_cluster,
    make_kb_entry,
    make_kb_match_result,
    make_triage_report,
)


def _extract_error_example_textarea(report_html: str) -> str:
    match = re.search(
        r'<textarea name="error_example" rows="4">(.*?)</textarea>',
        report_html,
        flags=re.DOTALL,
    )
    assert match is not None
    return _html.unescape(match.group(1))


def test_guided_onboarding_uses_project_learning_flow() -> None:
    """Guided mode shows project-learning actions instead of the normal KB block."""
    cluster = make_failure_cluster(cluster_id="c1", label="Payment timeout", member_count=5)
    result = AnalysisResult(
        triage_report=make_triage_report(project_id=42),
        clustering_report=make_clustering_report(
            clusters=[cluster],
            cluster_count=1,
            total_failures=5,
        ),
        kb_results={
            "c1": [
                make_kb_match_result(
                    entry=make_kb_entry(
                        title="Global timeout",
                        project_id=None,
                        entry_id=100,
                    )
                )
            ]
        },
        onboarding=OnboardingState(
            mode=OnboardingMode.GUIDED,
            needs_bootstrap=True,
            project_kb_entries=0,
            prioritized_cluster_ids=["c1"],
            starter_pack_available=True,
        ),
    )

    html = generate_html_report(
        result,
        feedback_api_url="http://feedback.local",
    )

    assert "Alla ещё не знает этот проект" in html
    assert "Создать решение для кластера" in html
    assert "Показать starter pack" in html
    assert '<div class="block-title">База знаний</div>' not in html


def test_guided_onboarding_canonicalizes_error_example_prefill() -> None:
    """KB create form uses normalized message+log without raw volatile values."""
    cluster = make_failure_cluster(
        cluster_id="c3",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message=(
            "Order 123e4567-e89b-12d3-a456-426614174000 failed "
            "at 2026-02-10 12:00:00 from 10.20.30.40"
        ),
        example_trace_snippet="at Service.java:42",
    )
    triage = make_triage_report(
        project_id=42,
        failed_tests=[
            make_failed_test_summary(
                test_result_id=1,
                log_snippet=(
                    "--- [файл: app.log] ---\n"
                    "2026-02-10 12:00:00 [ERROR] requestId="
                    "123e4567e89b12d3a456426614174000 from 10.20.30.40 build 123456"
                ),
            )
        ],
    )
    result = AnalysisResult(
        triage_report=triage,
        clustering_report=make_clustering_report(clusters=[cluster], cluster_count=1),
        onboarding=OnboardingState(
            mode=OnboardingMode.GUIDED,
            needs_bootstrap=True,
            prioritized_cluster_ids=["c3"],
        ),
    )

    report_html = generate_html_report(
        result,
        feedback_api_url="http://feedback.local",
    )
    textarea_value = _extract_error_example_textarea(report_html)

    assert "at Service.java:42" not in textarea_value
    assert "2026-02-10 12:00:00" not in textarea_value
    assert "123e4567-e89b-12d3-a456-426614174000" not in textarea_value
    assert "123e4567e89b12d3a456426614174000" not in textarea_value
    assert "10.20.30.40" not in textarea_value
    assert "<TS>" in textarea_value
    assert "<ID>" in textarea_value
    assert "<IP>" in textarea_value
    assert "<NUM>" in textarea_value


def test_html_report_shows_kb_setup_callout() -> None:
    """When KB is disabled, the report shows the setup callout."""
    result = AnalysisResult(
        triage_report=make_triage_report(),
        clustering_report=make_clustering_report(),
        onboarding=OnboardingState(mode=OnboardingMode.KB_NOT_CONFIGURED),
    )

    html = generate_html_report(result)

    assert "ALLURE_KB_POSTGRES_DSN" in html
    assert "Проектная память отключена" in html


def test_html_report_embeds_exact_feedback_payload() -> None:
    """Exact feedback entries render their badge and payload bootstrap data."""
    cluster = make_failure_cluster(cluster_id="c-exact")
    result = AnalysisResult(
        triage_report=make_triage_report(project_id=42),
        clustering_report=make_clustering_report(clusters=[cluster], cluster_count=1),
        kb_results={
            "c-exact": [
                make_kb_match_result(
                    score=0.33,
                    match_origin="feedback_exact",
                    feedback_vote="like",
                    feedback_id=55,
                    entry=make_kb_entry(entry_id=101, project_id=42, title="Confirmed KB"),
                )
            ]
        },
        feedback_contexts={
            "c-exact": FeedbackClusterContext(
                audit_text="[message]\nGateway timeout while saving order",
                issue_signature=FeedbackIssueSignature(
                    signature_hash="a" * 64,
                    basis="message_exact",
                ),
            )
        },
    )

    html = generate_html_report(
        result,
        feedback_api_url="http://feedback.local",
    )

    assert "Ранее подтверждено" in html
    assert "fb#55" in html
    assert "CLUSTER_FEEDBACK_CONTEXTS" in html
    assert "issue_signature_hash" in html


def test_html_report_collapses_cluster_test_list_after_first_three() -> None:
    """Clusters with many tests render all items but hide extras behind a toggle."""
    cluster = make_failure_cluster(
        cluster_id="c-tests",
        member_test_ids=[1, 2, 3, 4, 5],
        member_count=5,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    name="test_alpha",
                    link="https://example.local/testresult/1",
                ),
                make_failed_test_summary(
                    test_result_id=2,
                    name="test_beta",
                    link="https://example.local/testresult/2",
                ),
                make_failed_test_summary(
                    test_result_id=3,
                    name="test_gamma",
                    link="https://example.local/testresult/3",
                ),
                make_failed_test_summary(
                    test_result_id=4,
                    name="test_delta",
                    link="https://example.local/testresult/4",
                ),
                make_failed_test_summary(
                    test_result_id=5,
                    name="test_epsilon",
                    link="https://example.local/testresult/5",
                ),
            ]
        ),
        clustering_report=make_clustering_report(
            clusters=[cluster],
            cluster_count=1,
            total_failures=5,
        ),
    )

    html = generate_html_report(result)

    assert 'class="test-list collapsed"' in html
    assert 'class="test-list-toggle"' in html
    assert 'aria-expanded="false">Развернуть</button>' in html
    assert html.count('class="test-id test-id-extra"') == 2
    assert "test_alpha" in html
    assert "test_beta" in html
    assert "test_gamma" in html
    assert "test_delta" in html
    assert "test_epsilon" in html
    assert 'href="https://example.local/errors/4"' in html
    assert "https://example.local/testresult/4" not in html


def test_html_report_keeps_short_cluster_test_list_expanded() -> None:
    """Clusters with up to three tests render without collapse controls."""
    cluster = make_failure_cluster(
        cluster_id="c-short",
        member_test_ids=[1, 2, 3],
        member_count=3,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(test_result_id=1, name="test_one"),
                make_failed_test_summary(test_result_id=2, name="test_two"),
                make_failed_test_summary(test_result_id=3, name="test_three"),
            ]
        ),
        clustering_report=make_clustering_report(
            clusters=[cluster],
            cluster_count=1,
            total_failures=3,
        ),
    )

    html = generate_html_report(result)

    assert 'class="test-list-toggle"' not in html
    assert 'class="test-list collapsed"' not in html
    assert 'class="test-id test-id-extra"' not in html
    assert 'class="test-id no-link test-id-extra"' not in html
    assert "test_one" in html
    assert "test_two" in html
    assert "test_three" in html
