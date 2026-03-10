"""Тесты HTML-отчёта для guided onboarding."""

from __future__ import annotations

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


def test_guided_onboarding_hides_global_matches_from_primary_block() -> None:
    """В guided-режиме seeded/global KB не показывается как primary block."""
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

    assert "Алла ещё не знает этот проект" in html
    assert "Сначала инструмент показывает реальные кластеры ошибок" in html
    assert "Создать решение для проекта" in html
    assert "Показать starter pack" in html
    assert "Скопировать в проект" in html
    assert '<div class="block-title">База знаний</div>' not in html
    assert "Guided onboarding" not in html
    assert "С чего начать" not in html
    assert "Top 1 для onboarding" not in html
    assert "Global timeout" in html


def test_guided_onboarding_prefills_create_form_from_llm() -> None:
    """LLM-анализ предзаполняет category/description/steps в create form."""
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
                    "ЧТО ПРОВЕРИТЬ:\n"
                    "1. Проверить доступность gateway.\n"
                    "2. Сверить сетевые ошибки в логах."
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
    assert "Проверить доступность gateway." in html
    assert "Сверить сетевые ошибки в логах." in html
    assert 'class="create-kb-toggle create-kb-toggle-primary"' in html
    assert 'class="create-kb-field"' in html
    assert "Шаги по устранению:" in html
    assert ">основное поле<" in html
    assert "(основное поле):" not in html
    assert "(необязательно):" not in html


def test_html_report_shows_kb_setup_callout() -> None:
    """При отключенной KB отчёт показывает setup-callout вместо guided CTA."""
    result = AnalysisResult(
        triage_report=make_triage_report(),
        clustering_report=make_clustering_report(),
        onboarding=OnboardingState(mode=OnboardingMode.KB_NOT_CONFIGURED),
    )

    html = generate_html_report(result)

    assert "Alla ещё не может учиться на ваших кластерах" in html
    assert "ALLURE_KB_POSTGRES_DSN" in html
