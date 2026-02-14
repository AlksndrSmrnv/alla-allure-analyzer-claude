"""Тесты моделей данных LLM-анализа."""

from __future__ import annotations

import dataclasses

import pytest

from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, LLMPushResult


# ---------------------------------------------------------------------------
# LLMClusterAnalysis (Pydantic)
# ---------------------------------------------------------------------------


def test_llm_cluster_analysis_defaults() -> None:
    """analysis_text по умолчанию пустая строка, error — None."""
    a = LLMClusterAnalysis(cluster_id="c1")
    assert a.analysis_text == ""
    assert a.error is None


def test_llm_cluster_analysis_with_values() -> None:
    """Все поля заполняются корректно."""
    a = LLMClusterAnalysis(
        cluster_id="c2",
        analysis_text="NPE in UserService",
        error="timeout",
    )
    assert a.cluster_id == "c2"
    assert a.analysis_text == "NPE in UserService"
    assert a.error == "timeout"


def test_llm_cluster_analysis_roundtrip() -> None:
    """model_dump → model_validate сохраняет все поля."""
    original = LLMClusterAnalysis(cluster_id="c3", analysis_text="text")
    restored = LLMClusterAnalysis.model_validate(original.model_dump())
    assert restored == original


# ---------------------------------------------------------------------------
# LLMAnalysisResult (frozen dataclass)
# ---------------------------------------------------------------------------


def test_llm_analysis_result_frozen() -> None:
    """Frozen dataclass: попытка изменить поле → FrozenInstanceError."""
    result = LLMAnalysisResult(
        total_clusters=5,
        analyzed_count=3,
        failed_count=1,
        skipped_count=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.total_clusters = 10  # type: ignore[misc]


def test_llm_analysis_result_default_dict() -> None:
    """cluster_analyses по умолчанию — пустой dict."""
    result = LLMAnalysisResult(
        total_clusters=0,
        analyzed_count=0,
        failed_count=0,
        skipped_count=0,
    )
    assert result.cluster_analyses == {}


# ---------------------------------------------------------------------------
# LLMPushResult (frozen dataclass)
# ---------------------------------------------------------------------------


def test_llm_push_result_frozen() -> None:
    """Frozen dataclass: попытка изменить поле → FrozenInstanceError."""
    result = LLMPushResult(
        total_tests=10,
        updated_count=8,
        failed_count=1,
        skipped_count=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.updated_count = 99  # type: ignore[misc]


def test_llm_push_result_requires_all_fields() -> None:
    """Все поля обязательны — пропуск любого → TypeError."""
    with pytest.raises(TypeError):
        LLMPushResult(total_tests=10)  # type: ignore[call-arg]
