"""Pydantic-модели для результатов кластеризации ошибок тестов."""

from pydantic import BaseModel, Field


class ClusterSignature(BaseModel):
    """Сигнатура кластера — общие признаки, объединяющие тесты в группу."""

    exception_type: str | None = None
    message_pattern: str | None = None
    common_frames: list[str] = Field(default_factory=list)
    category: str | None = None
    representative_message: str | None = None


class ClusterExample(BaseModel):
    """Пример кластера для задания модели: роль и тест.

    Роли: ``typical`` — медоид (ближе всех к остальным участникам), ``different`` —
    самый далёкий от типичного, если он действительно отличается, ``informative`` —
    больше всего событий-ошибок в логе среди остальных, если его лог другой.
    """

    role: str
    test_result_id: int


class FailureCluster(BaseModel):
    """Кластер — группа тестов, упавших по одной причине."""

    cluster_id: str
    label: str
    signature: ClusterSignature
    member_test_ids: list[int] = Field(default_factory=list)
    member_count: int = 0
    representative_test_id: int | None = None
    example_message: str | None = None
    example_trace_snippet: str | None = None
    example_step_path: str | None = None
    example_correlation: str | None = None
    example_correlation_test_id: int | None = None
    # Примеры для задания (первый — типичный). Сигнатура и база знаний держатся на
    # representative_test_id.
    examples: list[ClusterExample] = Field(default_factory=list)


class ClusteringReport(BaseModel):
    """Результат кластеризации всех падений в рамках одного launch."""

    launch_id: int
    total_failures: int
    cluster_count: int
    clusters: list[FailureCluster] = Field(default_factory=list)
    unclustered_count: int = 0
