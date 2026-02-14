"""Тесты LLM-сервиса: построение промптов, анализ кластеров, push результатов."""

from __future__ import annotations

import pytest

from alla.knowledge.models import KBMatchResult, RootCauseCategory
from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster
from alla.models.common import TestStatus
from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, LLMPushResult
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.llm_service import (
    LLMService,
    _interpret_kb_score,
    build_cluster_prompt,
    format_llm_comment,
    push_llm_results,
)
from conftest import make_failure_cluster, make_kb_entry, make_kb_match_result


# ---------------------------------------------------------------------------
# _interpret_kb_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.9, "высокое совпадение"),
        (0.7, "высокое совпадение"),
        (0.5, "частичное совпадение"),
        (0.4, "частичное совпадение"),
        (0.3, "слабое совпадение"),
        (0.0, "слабое совпадение"),
    ],
)
def test_interpret_kb_score(score: float, expected: str) -> None:
    """Числовой score → текстовое описание уверенности."""
    assert _interpret_kb_score(score) == expected


# ---------------------------------------------------------------------------
# build_cluster_prompt
# ---------------------------------------------------------------------------


def test_prompt_includes_label_and_count() -> None:
    """Промпт содержит label и member_count кластера."""
    cluster = make_failure_cluster(label="TimeoutError in API", member_count=5)
    prompt = build_cluster_prompt(cluster)

    assert "TimeoutError in API" in prompt
    assert "5" in prompt


def test_prompt_includes_message_and_trace() -> None:
    """Промпт содержит example_message и example_trace_snippet."""
    cluster = make_failure_cluster(
        example_message="Expected 200 but got 500",
        example_trace_snippet="at ApiTest.java:33",
    )
    prompt = build_cluster_prompt(cluster)

    assert "Expected 200 but got 500" in prompt
    assert "at ApiTest.java:33" in prompt


def test_prompt_truncates_long_message() -> None:
    """message > 2000 символов → обрезается с суффиксом '...[обрезано]'."""
    long_msg = "x" * 3000
    cluster = make_failure_cluster(example_message=long_msg)
    prompt = build_cluster_prompt(cluster)

    assert "...[обрезано]" in prompt
    assert "x" * 2001 not in prompt


def test_prompt_truncates_long_trace() -> None:
    """trace > 3000 символов → обрезается с суффиксом '...[обрезано]'."""
    long_trace = "t" * 4000
    cluster = make_failure_cluster(example_trace_snippet=long_trace)
    prompt = build_cluster_prompt(cluster)

    assert "...[обрезано]" in prompt
    assert "t" * 3001 not in prompt


def test_prompt_includes_log_snippet() -> None:
    """log_snippet передан → включён в промпт."""
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, log_snippet="2024-01-01 [ERROR] boom")

    assert "2024-01-01 [ERROR] boom" in prompt
    assert "Фрагмент лога" in prompt


def test_prompt_omits_log_section_when_none() -> None:
    """log_snippet=None → секция 'Фрагмент лога' отсутствует."""
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, log_snippet=None)

    assert "Фрагмент лога" not in prompt


def test_prompt_includes_kb_matches_max_3() -> None:
    """Только первые 3 KB-совпадения включаются в промпт."""
    matches = [
        make_kb_match_result(entry=make_kb_entry(title=f"KB Entry {i}"), score=0.8 - i * 0.1)
        for i in range(5)
    ]
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=matches)

    assert "KB Entry 0" in prompt
    assert "KB Entry 1" in prompt
    assert "KB Entry 2" in prompt
    assert "KB Entry 3" not in prompt
    assert "KB Entry 4" not in prompt


def test_prompt_without_message_and_trace() -> None:
    """Кластер без message и trace → секции ошибки и трейса отсутствуют."""
    cluster = make_failure_cluster(
        example_message=None,
        example_trace_snippet=None,
    )
    prompt = build_cluster_prompt(cluster)

    assert "Сообщение об ошибке" not in prompt
    assert "Стек-трейс" not in prompt


def test_prompt_kb_matches_include_resolution_steps() -> None:
    """KB-совпадения включают шаги по устранению."""
    entry = make_kb_entry(
        title="DNS failure",
        resolution_steps=["Check DNS servers", "Restart pods"],
    )
    match = make_kb_match_result(entry=entry, score=0.75)
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Check DNS servers" in prompt
    assert "Restart pods" in prompt


# ---------------------------------------------------------------------------
# format_llm_comment
# ---------------------------------------------------------------------------


def test_format_llm_comment_has_header() -> None:
    """Комментарий начинается с '[alla] LLM-анализ ошибки' и содержит separator."""
    comment = format_llm_comment("Analysis text here")

    assert comment.startswith("[alla] LLM-анализ ошибки")
    assert "=" * 40 in comment
    assert "Analysis text here" in comment


# ---------------------------------------------------------------------------
# LLMService.analyze_clusters
# ---------------------------------------------------------------------------


def _make_report(*clusters: FailureCluster) -> ClusteringReport:
    return ClusteringReport(
        launch_id=1,
        total_failures=sum(c.member_count for c in clusters),
        cluster_count=len(clusters),
        clusters=list(clusters),
    )


@pytest.mark.asyncio
async def test_analyze_clusters_empty() -> None:
    """Пустой clustering_report → LLMAnalysisResult с нулевыми счётчиками."""

    class _Client:
        async def run_flow(self, input_value):
            raise AssertionError("Не должен вызываться")

    service = LLMService(_Client())  # type: ignore[arg-type]
    report = _make_report()
    report.clusters = []

    result = await service.analyze_clusters(report)

    assert result.total_clusters == 0
    assert result.analyzed_count == 0
    assert result.skipped_count == 0
    assert result.failed_count == 0


@pytest.mark.asyncio
async def test_analyze_clusters_skips_without_error_text() -> None:
    """Кластер без message и trace → skipped, analysis.error содержит причину."""

    class _Client:
        async def run_flow(self, input_value):
            raise AssertionError("Не должен вызываться")

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster(
        example_message=None,
        example_trace_snippet=None,
    )
    report = _make_report(cluster)

    result = await service.analyze_clusters(report)

    assert result.skipped_count == 1
    assert result.analyzed_count == 0
    analysis = result.cluster_analyses["c1"]
    assert "Нет текста" in analysis.error


@pytest.mark.asyncio
async def test_analyze_clusters_success() -> None:
    """Успешный анализ кластера → analyzed=1, analysis_text заполнен."""

    class _Client:
        async def run_flow(self, input_value):
            return "Root cause: NullPointerException in UserService"

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster()
    report = _make_report(cluster)

    result = await service.analyze_clusters(report)

    assert result.analyzed_count == 1
    assert result.failed_count == 0
    analysis = result.cluster_analyses["c1"]
    assert analysis.analysis_text == "Root cause: NullPointerException in UserService"
    assert analysis.error is None


@pytest.mark.asyncio
async def test_analyze_clusters_langflow_error_graceful() -> None:
    """run_flow raises → failed=1, analysis.error содержит текст ошибки."""

    class _Client:
        async def run_flow(self, input_value):
            raise RuntimeError("Langflow unavailable")

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster()
    report = _make_report(cluster)

    result = await service.analyze_clusters(report)

    assert result.failed_count == 1
    assert result.analyzed_count == 0
    analysis = result.cluster_analyses["c1"]
    assert "Langflow unavailable" in analysis.error


@pytest.mark.asyncio
async def test_analyze_clusters_uses_log_snippet_from_representative() -> None:
    """log_snippet берётся из representative test через test_by_id."""
    captured_prompts: list[str] = []

    class _Client:
        async def run_flow(self, input_value):
            captured_prompts.append(input_value)
            return "analysis"

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster(representative_test_id=42, member_test_ids=[42])
    report = _make_report(cluster)

    failed_tests = [
        FailedTestSummary(
            test_result_id=42,
            name="test",
            status=TestStatus.FAILED,
            log_snippet="ERROR connection refused",
        ),
    ]

    await service.analyze_clusters(report, failed_tests=failed_tests)

    assert len(captured_prompts) == 1
    assert "ERROR connection refused" in captured_prompts[0]


@pytest.mark.asyncio
async def test_analyze_clusters_multiple() -> None:
    """Несколько кластеров → все обрабатываются."""
    call_count = 0

    class _Client:
        async def run_flow(self, input_value):
            nonlocal call_count
            call_count += 1
            return f"analysis-{call_count}"

    service = LLMService(_Client())  # type: ignore[arg-type]
    c1 = make_failure_cluster(cluster_id="c1", member_test_ids=[1])
    c2 = make_failure_cluster(cluster_id="c2", member_test_ids=[2])
    report = _make_report(c1, c2)

    result = await service.analyze_clusters(report)

    assert result.total_clusters == 2
    assert result.analyzed_count == 2
    assert "c1" in result.cluster_analyses
    assert "c2" in result.cluster_analyses


# ---------------------------------------------------------------------------
# push_llm_results
# ---------------------------------------------------------------------------


def _make_test_data(
    cluster_ids_and_tests: dict[str, list[tuple[int, int | None]]],
    llm_texts: dict[str, str | None],
) -> tuple[ClusteringReport, LLMAnalysisResult, TriageReport]:
    """Собрать тестовые данные для push_llm_results."""
    clusters = []
    failed_tests = []
    analyses = {}

    for cid, tests in cluster_ids_and_tests.items():
        test_ids = [t[0] for t in tests]
        clusters.append(make_failure_cluster(
            cluster_id=cid,
            member_test_ids=test_ids,
            member_count=len(test_ids),
        ))
        for test_id, tc_id in tests:
            failed_tests.append(FailedTestSummary(
                test_result_id=test_id,
                name=f"test-{test_id}",
                status=TestStatus.FAILED,
                test_case_id=tc_id,
            ))

        text = llm_texts.get(cid)
        analyses[cid] = LLMClusterAnalysis(
            cluster_id=cid,
            analysis_text=text or "",
        )

    report = ClusteringReport(
        launch_id=1,
        total_failures=sum(len(t) for t in cluster_ids_and_tests.values()),
        cluster_count=len(clusters),
        clusters=clusters,
    )
    llm_result = LLMAnalysisResult(
        total_clusters=len(clusters),
        analyzed_count=sum(1 for t in llm_texts.values() if t),
        failed_count=0,
        skipped_count=0,
        cluster_analyses=analyses,
    )
    triage = TriageReport(
        launch_id=1,
        total_results=100,
        failed_tests=failed_tests,
    )
    return report, llm_result, triage


@pytest.mark.asyncio
async def test_push_deduplicates_by_test_case_id() -> None:
    """2 теста с одним test_case_id → только 1 вызов post_comment."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, llm_result, triage = _make_test_data(
        {"c1": [(1, 100), (2, 100)]},
        {"c1": "LLM analysis"},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert len(posted) == 1
    assert posted[0] == 100
    assert result.updated_count == 1


@pytest.mark.asyncio
async def test_push_skips_tests_without_test_case_id() -> None:
    """test_case_id=None → тест пропущен."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, llm_result, triage = _make_test_data(
        {"c1": [(1, None), (2, 200)]},
        {"c1": "LLM analysis"},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert len(posted) == 1
    assert posted[0] == 200
    assert result.skipped_count >= 1


@pytest.mark.asyncio
async def test_push_skips_cluster_without_analysis() -> None:
    """Кластер без analysis_text → все тесты пропущены."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, llm_result, triage = _make_test_data(
        {"c1": [(1, 100)]},
        {"c1": None},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert len(posted) == 0
    assert result.skipped_count >= 1


@pytest.mark.asyncio
async def test_push_error_resilience() -> None:
    """post_comment raises для одного → failed=1, остальные успешно обновлены."""

    class _Updater:
        async def post_comment(self, tc_id, body):
            if tc_id == 100:
                raise RuntimeError("API error")

    report, llm_result, triage = _make_test_data(
        {"c1": [(1, 100), (2, 200), (3, 300)]},
        {"c1": "analysis"},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert result.failed_count == 1
    assert result.updated_count == 2
