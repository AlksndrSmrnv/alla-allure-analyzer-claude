"""Поведенческие тесты генерации HTML-отчёта."""

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
    """Guided mode показывает действия обучения проекта вместо обычного KB-блока."""
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
    """Форма создания KB-записи использует normalized message+log без raw volatile values."""
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
    """Когда KB отключена, отчёт показывает setup callout."""
    result = AnalysisResult(
        triage_report=make_triage_report(),
        clustering_report=make_clustering_report(),
        onboarding=OnboardingState(mode=OnboardingMode.KB_NOT_CONFIGURED),
    )

    html = generate_html_report(result)

    assert "ALLURE_KB_POSTGRES_DSN" in html
    assert "Проектная память отключена" in html


def test_html_report_embeds_exact_feedback_payload() -> None:
    """Записи exact feedback рендерят badge и bootstrap payload."""
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

def test_html_report_renders_http_section_separately() -> None:
    """HTTP-секция выводится как отдельный блок, а не как сырой заголовок в pre."""
    cluster = make_failure_cluster(
        cluster_id="c-http",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    log_snippet=(
                        "--- [файл: app.log] ---\n"
                        "retry budget exhausted while saving order\n"
                        "\n"
                        "--- [HTTP: response.json] ---\n"
                        "HTTP статус: 503\n"
                        "error: Service unavailable"
                    ),
                )
            ]
        ),
        clustering_report=make_clustering_report(
            clusters=[cluster],
            cluster_count=1,
            total_failures=1,
        ),
    )

    html = generate_html_report(result)

    assert "HTTP: response.json" in html
    assert "Service unavailable" in html
    assert "--- [HTTP: response.json] ---" not in html


def test_html_report_renders_cluster_correlation_separately_from_log() -> None:
    cluster = make_failure_cluster(
        cluster_id="c-corr",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_correlation="operUID=239482348, rqUID=324234523420",
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    log_snippet=(
                        "--- [файл: app.log] ---\n"
                        "retry budget exhausted while saving order\n"
                    ),
                )
            ]
        ),
        clustering_report=make_clustering_report(
            clusters=[cluster],
            cluster_count=1,
            total_failures=1,
        ),
    )

    html = generate_html_report(result)

    assert "Корреляция" in html
    assert "operUID=239482348, rqUID=324234523420" in html
    assert "Лог приложения" in html


def test_html_report_renders_merge_controls_only_for_mergeable_clusters() -> None:
    """Merge checkbox рисуется только для кластеров с вычислимой сигнатурой."""
    cluster_a = make_failure_cluster(cluster_id="c-merge-a", member_count=2)
    cluster_b = make_failure_cluster(cluster_id="c-merge-b", member_count=1)
    cluster_empty = make_failure_cluster(
        cluster_id="c-no-signal",
        member_count=1,
        example_message=None,
        example_trace_snippet=None,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(project_id=42),
        clustering_report=make_clustering_report(
            clusters=[cluster_a, cluster_b, cluster_empty],
            cluster_count=3,
            total_failures=4,
        ),
        feedback_contexts={
            "c-merge-a": FeedbackClusterContext(
                audit_text="[message]\nGateway timeout",
                base_issue_signature=FeedbackIssueSignature(
                    signature_hash="a" * 64,
                    basis="message_exact",
                ),
            ),
            "c-merge-b": FeedbackClusterContext(
                audit_text="[message]\nConnection refused",
                base_issue_signature=FeedbackIssueSignature(
                    signature_hash="b" * 64,
                    basis="message_exact",
                ),
            ),
        },
    )

    html = generate_html_report(
        result,
        feedback_api_url="http://feedback.local",
    )

    assert html.count('class="cluster-merge-checkbox"') == 2
    assert "cluster-merge-toolbar" in html
    assert "/api/v1/merge-rules" in html
