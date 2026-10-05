"""Pydantic-модели для ответов Allure TestOps API и доменных объектов."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alla_core.models.common import TestStatus

StatusDetails = dict[str, Any]
ExecutionParameter = dict[str, Any]


class TestResultResponse(BaseModel):
    """Сырой результат теста из Allure TestOps API.

    Используется для двух эндпоинтов:
    - ``GET /api/testresult?launchId=X`` (пагинированный список, ``trace`` обычно пустой)
    - ``GET /api/testresult/{id}`` (индивидуальный результат, содержит top-level ``trace``)

    Поля намеренно Optional там, где API может их не вернуть,
    что делает модель устойчивой к вариациям API. ``extra="allow"``
    захватывает любые недокументированные поля без ошибок валидации.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    full_name: str | None = Field(None, alias="fullName")
    status: str | None = None
    status_details: StatusDetails | None = Field(None, alias="statusDetails")
    trace: str | None = None
    duration: int | None = None
    test_case_id: int | None = Field(None, alias="testCaseId")
    test_case_name: str | None = Field(None, alias="testCaseName")
    launch_id: int | None = Field(None, alias="launchId")
    created_date: int | None = Field(None, alias="createdDate")
    category: str | None = None
    muted: bool = False
    hidden: bool = False

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, v: object) -> str | None:
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, dict):
            return v.get("name") or str(v)
        return str(v)


class LaunchResponse(BaseModel):
    """Метаданные запуска из Allure TestOps API."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str | None = None
    closed: bool = False
    created_date: int | None = Field(None, alias="createdDate")
    project_id: int | None = Field(None, alias="projectId")


class AttachmentMeta(BaseModel):
    """Метаданные аттачмента.

    Используется для двух источников:
    - Из ``GET /api/testresult/attachment?testResultId={id}`` (содержит ``id``)
    - Из execution-шагов (содержит ``source``, deprecated)

    Поле ``id`` используется для скачивания через новый API.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int | None = None
    name: str | None = None
    source: str | None = None
    type: str | None = None
    size: int | None = None
    content_type: str | None = Field(None, alias="contentType")


class CommentResponse(BaseModel):
    """Комментарий к тест-кейсу из Allure TestOps API.

    Используется для ``GET /api/comment?testCaseId={id}`` и
    ``DELETE /api/comment/{id}``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    body: str | None = None
    test_case_id: int | None = Field(None, alias="testCaseId")


class ExecutionStep(BaseModel):
    """Шаг выполнения теста из ``/api/testresult/{id}/execution``.

    Ответ эндпоинта — дерево шагов. Каждый шаг может содержать вложенные
    ``steps``, а также ``statusDetails`` с сообщением об ошибке и стек-трейсом.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str | None = None
    status: str | None = None
    status_details: StatusDetails | None = Field(None, alias="statusDetails")
    message: str | None = None
    trace: str | None = None
    steps: "list[ExecutionStep] | None" = None
    duration: int | None = None
    parameters: list[ExecutionParameter] | None = None
    attachments: list[AttachmentMeta] | None = None


class LogAttachmentRef(BaseModel):
    """Вложение, из которого в ``log_snippet`` попала секция (имя — как в её заголовке)."""

    id: int
    name: str


class AttemptSummary(BaseModel):
    """Скрытая (``hidden``) попытка того же выполнения теста, что и финальный результат.

    ``message`` — первая строка ошибки попытки (``None`` — не загружена или её нет);
    ``same_as_final`` — та же ли ошибка, что у финального результата (``repeat_key``:
    без времени, UUID и длинных чисел, но с кодами ошибок); ``None`` — сравнить нечем.
    """

    test_result_id: int
    status: TestStatus
    message: str | None = None
    same_as_final: bool | None = None


class PassedAfterRetry(BaseModel):
    """Тест, который прошёл после неудачных попыток: в активные кластеры не входит."""

    test_result_id: int
    name: str
    full_name: str | None = None
    link: str | None = None
    failed_attempts: int
    message: str | None = None


class RetryInfo(BaseModel):
    """Связь попыток с финальными результатами — только числа и имя поля связи.

    ``linked_by`` — поле, по которому связаны попытки (``historyId``, ``historyKey``,
    ``testCaseId+parameters+environment``), ``None`` — связать было не по чему.
    Скрытые попытки делятся на ``linked`` и несвязанные: ``no_key`` (нет значения
    ключа), ``no_final`` (нет финального результата с тем же ключом), ``ambiguous``
    (финальных результатов с этим ключом несколько). ``errors_total`` — неудачные
    попытки активных падений, чьи ошибки нужны (не больше 5 на тест);
    ``errors_known`` — у скольких из них ошибка известна; ``errors_capped`` — сколько не
    запрошено из-за общего потолка запросов.
    """

    linked_by: str | None = None
    hidden_total: int = 0
    linked: int = 0
    no_key: int = 0
    no_final: int = 0
    ambiguous: int = 0
    errors_total: int = 0
    errors_known: int = 0
    errors_capped: int = 0
    passed_after_retry: list[PassedAfterRetry] = Field(default_factory=list)


class FailedTestSummary(BaseModel):
    """Доменная модель: краткое описание упавшего теста для вывода триажа.

    ``status_message`` и ``status_trace`` заполняются трёхуровневым fallback:
    1. Из execution-шагов (``GET /api/testresult/{id}/execution``).
    2. Из ``statusDetails`` результата (пагинированный список).
    3. Из top-level ``trace`` индивидуального результата (``GET /api/testresult/{id}``).
    """

    test_result_id: int
    name: str
    full_name: str | None = None
    status: TestStatus
    category: str | None = None
    status_message: str | None = None
    status_trace: str | None = None
    execution_steps: list[ExecutionStep] | None = None
    test_case_id: int | None = None
    link: str | None = None
    duration_ms: int | None = None
    test_start_ms: int | None = None
    log_snippet: str | None = None
    # Enriched models используются в prepare до сериализации; feedback берёт
    # сохранённую entry.signature, а run.json не хранит контекст отбора лога.
    # Full model roundtrip сохраняет сигнатуру только для необрезанного лога.
    log_selection_error: str | None = Field(default=None, exclude=True)
    log_selection_truncated: bool = Field(default=False, exclude=True)
    # Происхождение секций лога: id вложения по имени из заголовка. Строки источника —
    # пометки «[строки a–b]» в начале блоков. В run.json не хранится (решение — шаг 3).
    log_attachments: list[LogAttachmentRef] = Field(default_factory=list, exclude=True)
    correlation_hint: str | None = None
    failed_step_path: str | None = None
    # Попытки до финального результата (шаг 5): последние MAX_ATTEMPTS_PER_TEST по
    # порядку; ``attempts_omitted`` — сколько более ранних не показано. В сигнатуре и
    # кластеризации не участвуют.
    attempts: list[AttemptSummary] = Field(default_factory=list)
    attempts_omitted: int = 0


class TriageReport(BaseModel):
    """Результат шага триажа: сводка падений запуска.

    ``failed_count`` и ``broken_count`` — общее число тестов с этими статусами
    (включая muted). ``muted_failure_count`` — сколько из них muted и исключено
    из анализа. ``failed_tests`` содержит только не-muted падения.

    Инвариант: ``len(failed_tests) == failure_count - muted_failure_count``.
    """

    launch_id: int
    launch_name: str | None = None
    project_id: int | None = None
    total_results: int
    passed_count: int = 0
    failed_count: int = 0
    broken_count: int = 0
    skipped_count: int = 0
    unknown_count: int = 0
    muted_failure_count: int = 0
    failed_tests: list[FailedTestSummary] = []
    retries: RetryInfo = Field(default_factory=RetryInfo)

    @property
    def failure_count(self) -> int:
        """Общее число падений (failed + broken), включая muted."""
        return self.failed_count + self.broken_count

    @property
    def active_failure_count(self) -> int:
        """Число падений, участвующих в анализе (без muted)."""
        return self.failure_count - self.muted_failure_count
