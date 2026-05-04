"""Регрессионные тесты для агентского адаптера.

Главное инвариант: вывод адаптера структурно совпадает с тем, что
кладёт в :class:`LLMAnalysisResult` / :class:`LLMLaunchSummary`
серверный путь GigaChat. Иначе skill-режим даст другой HTML-отчёт и
другой launch summary user_prompt при тех же данных.
"""

from __future__ import annotations

from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla.services.agent_analysis_adapter import (
    agent_to_launch_summary,
    agent_to_llm_result,
)


def _single_cluster_report(cluster_id: str = "c-1") -> ClusteringReport:
    return ClusteringReport(
        launch_id=1,
        total_failures=1,
        cluster_count=1,
        unclustered_count=0,
        clusters=[
            FailureCluster(
                cluster_id=cluster_id,
                label="cluster",
                signature=ClusterSignature(),
                member_test_ids=[1],
                member_count=1,
            )
        ],
    )


def test_agent_to_llm_result_does_not_append_recommendations() -> None:
    """`recommendations` остаются в схеме, но в `analysis_text` не дописываются.

    GigaChat-путь возвращает голый текст из cluster-analysis промпта;
    skill-адаптер должен делать то же самое, чтобы launch summary
    user_prompt и HTML-отчёт совпадали с серверными.
    """
    payload = {
        "schema_version": 1,
        "clusters": {
            "c-1": {
                "category": "service",
                "confidence": "high",
                "analysis_text": "ЧТО СЛОМАЛОСЬ: x\n\nПРИЧИНА: y\n\nКАК ИСПРАВИТЬ:\n1. step",
                "recommendations": ["never appended", "should not appear"],
            }
        },
    }

    result = agent_to_llm_result(payload, _single_cluster_report())

    text = result.cluster_analyses["c-1"].analysis_text
    assert "Рекомендации" not in text
    assert "never appended" not in text
    assert text.endswith("1. step")


def test_agent_to_launch_summary_returns_canonical_summary_text() -> None:
    """Структурные поля игнорируются — рендерится только `summary_text`."""
    payload = {
        "launch_summary": {
            "summary_text": "Прогон провалился по двум кластерам.",
            "key_findings": ["X", "Y"],
            "priority_actions": ["A", "B"],
            "unanalyzed_tail": {"cluster_count": 5, "test_count": 20, "note": "tail"},
        }
    }

    summary = agent_to_launch_summary(payload)

    assert summary.summary_text == "Прогон провалился по двум кластерам."
    assert "Ключевые наблюдения" not in summary.summary_text
    assert "Приоритетные действия" not in summary.summary_text
    assert "Не проанализировано" not in summary.summary_text


def test_agent_to_llm_result_handles_unanalyzed_without_recommendations() -> None:
    """Категория `unanalyzed` помечает кластер как skipped с error-текстом."""
    payload = {
        "schema_version": 1,
        "clusters": {
            "c-1": {
                "category": "unanalyzed",
                "confidence": "low",
                "analysis_text": "tail (size=4)",
                "recommendations": [],
            }
        },
    }

    result = agent_to_llm_result(payload, _single_cluster_report())

    cluster = result.cluster_analyses["c-1"]
    assert cluster.analysis_text == ""
    assert cluster.error == "tail (size=4)"
    assert result.skipped_count == 1
    assert result.analyzed_count == 0
