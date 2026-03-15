"""Тесты HTML-отчёта для guided onboarding."""

from __future__ import annotations

import html as _html
import re

from alla.knowledge.feedback_models import FeedbackClusterContext, FeedbackIssueSignature
from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis
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
    """Достать canonicalized содержимое textarea error_example из HTML-формы."""
    match = re.search(
        r'<textarea name="error_example" rows="4">(.*?)</textarea>',
        report_html,
        flags=re.DOTALL,
    )
    assert match is not None
    return _html.unescape(match.group(1))


def test_guided_onboarding_hides_global_matches_from_primary_block() -> None:
    """В guided-режиме seeded/global база знаний не показывается как primary block."""
    cluster = make_failure_cluster(cluster_id="c1", label="Payment timeout", member_count=5)
    triage = make_triage_report(
        project_id=42,
        failed_tests=[
            make_failed_test_summary(
                test_result_id=1,
                log_snippet="gateway timeout log",
            )
        ],
    )
    result = AnalysisResult(
        triage_report=triage,
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
    assert "Алла" not in html
    assert "Сначала инструмент показывает реальные кластеры ошибок" in html
    assert "Следующий шаг - ваш" in html
    assert "Опишите каждый кластер и добавьте для него решение" in html
    assert "Создать решение для кластера" in html
    assert "Создать решение для проекта" not in html
    assert "Показать starter pack" in html
    assert "Скопировать в проект" in html
    assert '<div class="block-title">База знаний</div>' not in html
    assert "Guided onboarding" not in html
    assert "С чего начать" not in html
    assert "Top 1 для onboarding" not in html
    assert "Global timeout" in html


def test_guided_onboarding_prefills_create_form_from_llm() -> None:
    """LLM-анализ предзаполняет category и description, но steps остаются пустыми."""
    cluster = make_failure_cluster(cluster_id="c2", label="Gateway timeout")
    triage = make_triage_report(project_id=42)
    llm_result = LLMAnalysisResult(
        total_clusters=1,
        analyzed_count=1,
        failed_count=0,
        skipped_count=0,
        cluster_analyses={
            "c2": LLMClusterAnalysis(
                cluster_id="c2",
                analysis_text=(
                    "ЧТО СЛОМАЛОСЬ: Платёжный шлюз не ответил вовремя.\n"
                    "ПРИЧИНА: окружение — внешний gateway недоступен.\n"
                    "КАК ИСПРАВИТЬ:\n"
                    "1. Перезапустить или восстановить внешний gateway.\n"
                    "2. Починить сетевую доступность до gateway."
                ),
            )
        },
    )
    result = AnalysisResult(
        triage_report=triage,
        clustering_report=make_clustering_report(clusters=[cluster], cluster_count=1),
        llm_result=llm_result,
        onboarding=OnboardingState(
            mode=OnboardingMode.GUIDED,
            needs_bootstrap=True,
            prioritized_cluster_ids=["c2"],
        ),
    )

    html = generate_html_report(
        result,
        feedback_api_url="http://feedback.local",
    )

    assert '<option value="env" selected="selected">env</option>' in html
    assert "Платёжный шлюз не ответил вовремя." in html
    assert "Перезапустить или восстановить внешний gateway." in html
    assert "Починить сетевую доступность до gateway." in html
    assert 'class="create-kb-toggle create-kb-toggle-primary"' in html
    assert 'class="create-kb-field"' in html
    assert "Шаги по устранению:" in html
    assert 'placeholder="Шаг 1&#10;Шаг 2&#10;Шаг 3" autofocus></textarea>' in html
    assert ">основное поле<" in html
    assert "(основное поле):" not in html
    assert "(необязательно):" not in html

def test_guided_onboarding_canonicalizes_error_example_prefill() -> None:
    """Форма базы знаний показывает normalized message+log без UI-маркеров и raw volatile-данных."""
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

    assert "--- Лог приложения ---" not in textarea_value
    assert "at Service.java:42" not in textarea_value
    assert "2026-02-10 12:00:00" not in textarea_value
    assert "123e4567-e89b-12d3-a456-426614174000" not in textarea_value
    assert "123e4567e89b12d3a456426614174000" not in textarea_value
    assert "10.20.30.40" not in textarea_value
    assert "<TS>" in textarea_value
    assert "<ID>" in textarea_value
    assert "<IP>" in textarea_value
    assert "<NUM>" in textarea_value
    assert "Order <ID> failed at <TS> from <IP>" in textarea_value


def test_html_report_shows_kb_setup_callout() -> None:
    """При отключенной базе знаний отчёт показывает setup-callout вместо guided CTA."""
    result = AnalysisResult(
        triage_report=make_triage_report(),
        clustering_report=make_clustering_report(),
        onboarding=OnboardingState(mode=OnboardingMode.KB_NOT_CONFIGURED),
    )

    html = generate_html_report(result)

    assert "Alla ещё не может учиться на ваших кластерах" in html
    assert "ALLURE_KB_POSTGRES_DSN" in html


def test_html_report_shows_exact_feedback_badge_and_context_payload() -> None:
    """Exact feedback memory показывает отдельный badge и новый JS payload."""
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
    assert "33%" not in html
    assert "fb#55" in html
    assert "CLUSTER_FEEDBACK_CONTEXTS" in html
    assert "issue_signature_hash" in html
    assert '"version": 3' in html
