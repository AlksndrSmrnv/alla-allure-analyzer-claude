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


def test_html_report_rerun_button_opens_attached_new_report() -> None:
    """Rerun-кнопка ждёт новый report URL и открывает его отдельным кликом."""
    result = AnalysisResult(
        triage_report=make_triage_report(launch_id=123),
        clustering_report=make_clustering_report(launch_id=123),
    )

    html = generate_html_report(
        result,
        server_url="https://alla.example",
    )

    assert "push_comments=false&push_report_link=true" in html
    assert "push_report_link=false" not in html
    assert "Открыть новый анализ" in html
    assert "is-ready" in html
    assert "X-Report-URL" in html
    assert "window.location.href" in html
    assert "document.write(result.html)" in html


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


def test_html_report_feedback_fetches_go_through_retry_wrapper() -> None:
    """Все feedback/KB fetch проходят через fetchWithRetry — лечит протухший TCP-пул."""
    result = AnalysisResult(
        triage_report=make_triage_report(),
        clustering_report=make_clustering_report(),
    )

    html = generate_html_report(result, feedback_api_url="http://feedback.local")

    assert "function fetchWithRetry" in html
    # Все feedback/KB вызовы внутри _build_feedback_js должны идти через
    # fetchWithRetry. Регулярка устойчива к пробелам и любому хвосту URL.
    assert not re.search(r"\bfetch\s*\(\s*FEEDBACK_API_URL\b", html)
    assert not re.search(r"\bfetch\s*\(\s*apiUrl\b", html)


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
        example_correlation_test_id=1,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    name="test_order_save",
                    link="https://allure.example/launch/1/testresult/1",
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
    assert 'class="correlation-source"' in html
    assert "https://allure.example/launch/1/errors/1" in html
    assert "test_order_save" in html


def test_html_report_correlation_without_source_link_skips_anchor() -> None:
    cluster = make_failure_cluster(
        cluster_id="c-corr-nolink",
        representative_test_id=2,
        member_test_ids=[2],
        member_count=1,
        example_correlation="operUID=op-x, rqUID=req-x",
        example_correlation_test_id=2,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=2,
                    name="test_no_link",
                    link=None,
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

    assert "operUID=op-x, rqUID=req-x" in html
    assert 'class="correlation-source"' not in html


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


def test_html_report_renders_expandable_full_stacktrace() -> None:
    """Полный status_trace представителя кластера доступен под кнопкой."""
    full_trace = (
        "java.lang.NullPointerException: Cannot invoke method on null\n"
        "\tat com.example.Service.process(Service.java:42)\n"
        "\tat com.example.Service.run(Service.java:21)\n"
        "\tat com.example.Main.main(Main.java:7)"
    )
    cluster = make_failure_cluster(
        cluster_id="c-trace",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message="NPE at Service.process",
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    status_message="NPE at Service.process",
                    status_trace=full_trace,
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

    assert "error-trace-toggle" in html
    assert "Показать полный стектрейс" in html
    assert 'class="error-trace hidden"' in html
    assert "com.example.Service.process(Service.java:42)" in html
    assert "com.example.Main.main(Main.java:7)" in html


def test_html_report_renders_trace_toggle_when_only_trace_is_available() -> None:
    """Trace-only кластер (без message/correlation/log) всё равно показывает блок и кнопку."""
    full_trace = (
        "java.lang.IllegalStateException: connection closed\n"
        "\tat com.example.Pool.borrow(Pool.java:88)\n"
        "\tat com.example.Worker.run(Worker.java:17)"
    )
    cluster = make_failure_cluster(
        cluster_id="c-trace-only",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message=None,
        example_trace_snippet="at com.example.Pool.borrow(Pool.java:88)",
        example_correlation=None,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    status_message=None,
                    status_trace=full_trace,
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

    assert '<div class="block-title">Пример ошибки</div>' in html
    assert '<button class="error-trace-toggle"' in html
    assert "Показать полный стектрейс" in html
    assert "com.example.Pool.borrow(Pool.java:88)" in html
    assert "com.example.Worker.run(Worker.java:17)" in html


def test_html_report_skips_trace_toggle_when_trace_equals_message() -> None:
    """Если status_trace дублирует message, кнопка раскрытия не рендерится."""
    same_text = "AssertionError: expected 200 but got 404"
    cluster = make_failure_cluster(
        cluster_id="c-no-trace",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
        example_message=same_text,
    )
    result = AnalysisResult(
        triage_report=make_triage_report(
            failed_tests=[
                make_failed_test_summary(
                    test_result_id=1,
                    status_message=same_text,
                    status_trace=same_text,
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

    assert '<button class="error-trace-toggle"' not in html
    assert "Показать полный стектрейс" not in html
