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


class ClusteringGateStats(BaseModel):
    """Сколько пар падений решил каждый gate кластеризации — только числа.

    Пары — среди падений с текстом. Пару засчитывает первый gate, который её решил, в
    порядке применения: assertion → шаг → лог → сообщение (ресурсы, log override).
    ``log_held_by_key`` — общего блока нет, общий ключ (корневой класс, код) держит пару с
    непохожими логами; ``log_held_by_block`` — без общих блоков логи разделились бы (общий
    фон или общая ошибка — по паре не отличить). ``*_merged`` — из них в одном кластере.
    """

    pairs: int = 0
    pairs_in_one_problem: int = 0
    assertion_split: int = 0
    step_split: int = 0
    log_pairs: int = 0
    log_split: int = 0
    log_held_by_key: int = 0
    log_held_by_block: int = 0
    log_held_merged: int = 0
    message_split: int = 0
    resource_split: int = 0
    log_override: int = 0
    log_override_resources: int = 0
    log_override_merged: int = 0
    # Пары id тестов для сверки с разметкой эталона; в run.json не пишутся.
    held_test_pairs: list[tuple[int, int]] = Field(default_factory=list, exclude=True)
    override_test_pairs: list[tuple[int, int]] = Field(default_factory=list, exclude=True)


class ClusteringReport(BaseModel):
    """Результат кластеризации всех падений в рамках одного launch."""

    launch_id: int
    total_failures: int
    cluster_count: int
    clusters: list[FailureCluster] = Field(default_factory=list)
    unclustered_count: int = 0
    gates: ClusteringGateStats = Field(default_factory=ClusteringGateStats)
