"""Pydantic-модели для результатов кластеризации ошибок тестов."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClusterSignature(BaseModel):
    """Сигнатура кластера — общие признаки, объединяющие тесты в группу."""

    exception_type: str | None = None
    message_pattern: str | None = None
    common_frames: list[str] = Field(default_factory=list)
    category: str | None = None


class FailureCluster(BaseModel):
    """Кластер — группа тестов, упавших по одной причине."""

    cluster_id: str
    label: str
    signature: ClusterSignature
    member_test_ids: list[int] = Field(default_factory=list)
    member_count: int = 0
    example_message: str | None = None
    example_trace_snippet: str | None = None


class ClusteringReport(BaseModel):
    """Результат кластеризации всех падений в рамках одного launch."""

    launch_id: int
    total_failures: int
    cluster_count: int
    clusters: list[FailureCluster] = Field(default_factory=list)
    unclustered_count: int = 0
