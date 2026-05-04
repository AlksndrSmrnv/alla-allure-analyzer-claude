"""Поведенческие тесты контрактов LLM-сервиса."""

from __future__ import annotations

import asyncio

import pytest

from alla.clients.gigachat_client import ChatResponse
from alla.models.clustering import ClusteringReport, FailureCluster
from alla.models.common import TestStatus as StatusEnum
from alla.models.llm import LLMAnalysisResult, LLMClusterAnalysis, TokenUsage
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.llm_service import (
    LLMService,
    build_cluster_prompt,
    build_launch_summary_prompt,
    push_llm_results,
)
from conftest import make_failure_cluster, make_kb_match_result, make_triage_report


def _make_report(*clusters: FailureCluster) -> ClusteringReport:
    return ClusteringReport(
        launch_id=1,
        total_failures=sum(cluster.member_count for cluster in clusters),
        cluster_count=len(clusters),
        clusters=list(clusters),
    )


def _chat_response(text: str = "analysis", prompt: int = 10, completion: int = 5, total: int = 15) -> ChatResponse:
    """Создать ChatResponse с заданными токенами."""
    return ChatResponse(text=text, token_usage=TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total))


def test_build_cluster_prompt_smoke_and_truncation() -> None:
    """Prompt сохраняет основной контекст кластера и обрезает слишком длинный message."""
    cluster = make_failure_cluster(
        label="Gateway timeout",
        member_count=3,
        example_message="x" * 3000,
    )

    system, user = build_cluster_prompt(cluster)

    assert "инженер" in system
    assert "Gateway timeout" in user
    assert "3" in user
    assert "...[обрезано]" in user
    assert "x" * 2001 not in user


def test_build_cluster_prompt_includes_kb_provenance_context() -> None:
    """Prompt объясняет, что KB match построен из объединённых message/trace/log."""
    cluster = make_failure_cluster(
        example_message="AssertionError: expected 200 but got 500",
    )

    _system, user = build_cluster_prompt(
        cluster,
        kb_matches=[make_kb_match_result()],
        kb_query_provenance=(34, 78, 96),
    )

    assert "AssertionError: expected 200 but got 500" in user
    assert "сообщение об ошибке (34 симв.)" in user
    assert "стек-трейс (78 симв.)" in user
    assert "лог приложения (96 симв.)" in user


def test_build_launch_summary_prompt_smoke() -> None:
    """Launch summary prompt включает данные кластеров и просит приоритизировать fixes."""
    cluster = make_failure_cluster(label="Gateway timeout", member_count=3)
    clustering_report = _make_report(cluster)
    triage_report = make_triage_report(total_results=10, failed_count=3)

    system, user = build_launch_summary_prompt(clustering_report, triage_report)

    assert "инженер" in system
    assert "Gateway timeout" in user
    assert "Приоритетные исправления" in user


@pytest.mark.asyncio
async def test_analyze_clusters_skips_without_error_text() -> None:
    """Кластер без message/trace/log пропускается без вызова LLM."""

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            raise AssertionError("chat should not be called")

    service = LLMService(_Client(), request_delay=0)  # type: ignore[arg-type]
    cluster = make_failure_cluster(
        example_message=None,
        example_trace_snippet=None,
    )

    result = await service.analyze_clusters(_make_report(cluster))

    assert result.skipped_count == 1
    assert result.analyzed_count == 0
    assert "Нет текста" in (result.cluster_analyses["c1"].error or "")


@pytest.mark.asyncio
async def test_analyze_clusters_success_uses_representative_log() -> None:
    """Лог representative test включается в LLM-запрос."""
    captured_prompts: list[str] = []

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            captured_prompts.append(user_prompt)
            return _chat_response("analysis")

    service = LLMService(_Client(), request_delay=0)  # type: ignore[arg-type]
    cluster = make_failure_cluster(representative_test_id=42, member_test_ids=[42])
    failed_tests = [
        FailedTestSummary(
            test_result_id=42,
            name="test",
            status=StatusEnum.FAILED,
            log_snippet="ERROR connection refused",
        ),
    ]

    result = await service.analyze_clusters(
        _make_report(cluster),
        failed_tests=failed_tests,
    )

    assert result.analyzed_count == 1
    assert result.failed_count == 0
    assert result.cluster_analyses["c1"].analysis_text == "analysis"
    assert len(captured_prompts) == 1
    assert "ERROR connection refused" in captured_prompts[0]


@pytest.mark.asyncio
async def test_analyze_clusters_handles_llm_error() -> None:
    """Per-cluster LLM failures сохраняются, не прерывая весь batch."""

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            raise RuntimeError("GigaChat unavailable")

    service = LLMService(_Client(), request_delay=0)  # type: ignore[arg-type]
    result = await service.analyze_clusters(_make_report(make_failure_cluster()))

    assert result.analyzed_count == 0
    assert result.failed_count == 1
    assert "GigaChat unavailable" in (result.cluster_analyses["c1"].error or "")


# ---------------------------------------------------------------------------
# Агрегация token usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_clusters_aggregates_token_usage() -> None:
    """Статистика токенов суммируется по всем успешно проанализированным кластерам."""
    call_count = 0

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            nonlocal call_count
            call_count += 1
            return _chat_response(
                f"analysis-{call_count}",
                prompt=100 * call_count,
                completion=50 * call_count,
                total=150 * call_count,
            )

    service = LLMService(_Client(), concurrency=1, request_delay=0)  # type: ignore[arg-type]
    c1 = make_failure_cluster(cluster_id="c1", member_test_ids=[1], member_count=1)
    c2 = make_failure_cluster(cluster_id="c2", member_test_ids=[2], member_count=1)

    result = await service.analyze_clusters(_make_report(c1, c2))

    assert result.analyzed_count == 2
    # Сумма: 100+200=300 prompt, 50+100=150 completion, 150+300=450 total
    assert result.token_usage.prompt_tokens == 300
    assert result.token_usage.completion_tokens == 150
    assert result.token_usage.total_tokens == 450


@pytest.mark.asyncio
async def test_generate_launch_summary_includes_token_usage() -> None:
    """generate_launch_summary записывает token_usage из ответа LLM."""

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            return _chat_response("summary text", prompt=200, completion=80, total=280)

    service = LLMService(_Client(), request_delay=0)  # type: ignore[arg-type]
    cluster = make_failure_cluster()
    report = _make_report(cluster)
    triage = make_triage_report()

    summary = await service.generate_launch_summary(report, triage)

    assert summary.summary_text == "summary text"
    assert summary.token_usage.prompt_tokens == 200
    assert summary.token_usage.completion_tokens == 80
    assert summary.token_usage.total_tokens == 280


# ---------------------------------------------------------------------------
# Ограничитель частоты запросов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_enforces_delay() -> None:
    """Между стартами запросов выдерживается минимальный интервал."""
    timestamps: list[float] = []

    class _Client:
        async def chat(self, system_prompt, user_prompt):
            timestamps.append(asyncio.get_running_loop().time())
            return _chat_response("ok")

    delay = 0.15
    service = LLMService(_Client(), concurrency=1, request_delay=delay)  # type: ignore[arg-type]
    c1 = make_failure_cluster(cluster_id="c1", member_test_ids=[1], member_count=1)
    c2 = make_failure_cluster(cluster_id="c2", member_test_ids=[2], member_count=1)
    c3 = make_failure_cluster(cluster_id="c3", member_test_ids=[3], member_count=1)

    await service.analyze_clusters(_make_report(c1, c2, c3))

    assert len(timestamps) == 3
    for i in range(1, len(timestamps)):
        elapsed = timestamps[i] - timestamps[i - 1]
        assert elapsed >= delay * 0.9, f"Interval {i}: {elapsed:.3f}s < {delay}s"


# ---------------------------------------------------------------------------
# Push LLM-результатов
# ---------------------------------------------------------------------------


def _make_push_inputs(
    cluster_ids_and_tests: dict[str, list[tuple[int, int | None]]],
    llm_texts: dict[str, str | None],
) -> tuple[ClusteringReport, LLMAnalysisResult, TriageReport]:
    clusters: list[FailureCluster] = []
    failed_tests: list[FailedTestSummary] = []
    analyses: dict[str, LLMClusterAnalysis] = {}

    for cluster_id, tests in cluster_ids_and_tests.items():
        test_ids = [test_id for test_id, _test_case_id in tests]
        clusters.append(
            make_failure_cluster(
                cluster_id=cluster_id,
                member_test_ids=test_ids,
                member_count=len(test_ids),
            )
        )
        for test_id, test_case_id in tests:
            failed_tests.append(
                FailedTestSummary(
                    test_result_id=test_id,
                    name=f"test-{test_id}",
                    status=StatusEnum.FAILED,
                    test_case_id=test_case_id,
                )
            )
        analyses[cluster_id] = LLMClusterAnalysis(
            cluster_id=cluster_id,
            analysis_text=llm_texts.get(cluster_id) or "",
        )

    return (
        ClusteringReport(
            launch_id=1,
            total_failures=sum(len(tests) for tests in cluster_ids_and_tests.values()),
            cluster_count=len(clusters),
            clusters=clusters,
        ),
        LLMAnalysisResult(
            total_clusters=len(clusters),
            analyzed_count=sum(1 for text in llm_texts.values() if text),
            failed_count=0,
            skipped_count=0,
            cluster_analyses=analyses,
        ),
        TriageReport(
            launch_id=1,
            total_results=100,
            failed_tests=failed_tests,
        ),
    )


@pytest.mark.asyncio
async def test_push_llm_results_deduplicates_by_test_case_id() -> None:
    """Два failed result с одним test_case_id дают один комментарий."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, test_case_id, body):
            posted.append(test_case_id)

    report, llm_result, triage = _make_push_inputs(
        {"c1": [(1, 100), (2, 100)]},
        {"c1": "LLM analysis"},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert posted == [100]
    assert result.updated_count == 1


@pytest.mark.asyncio
async def test_push_llm_results_is_error_resilient() -> None:
    """Один упавший post_comment не блокирует остальные."""

    class _Updater:
        async def post_comment(self, test_case_id, body):
            if test_case_id == 100:
                raise RuntimeError("API error")

    report, llm_result, triage = _make_push_inputs(
        {"c1": [(1, 100), (2, 200), (3, 300)]},
        {"c1": "analysis"},
    )

    result = await push_llm_results(report, llm_result, triage, _Updater())  # type: ignore[arg-type]

    assert result.failed_count == 1
    assert result.updated_count == 2


def test_build_cluster_prompt_respects_custom_limits() -> None:
    """build_cluster_prompt использует переданные message_max_chars/trace_max_chars."""
    cluster = make_failure_cluster(
        label="Timeout",
        example_message="A" * 100,
    )

    _sys_default, user_default = build_cluster_prompt(cluster)
    _sys_tight, user_tight = build_cluster_prompt(cluster, message_max_chars=50)

    # При жёстком лимите 100 символов обрезаются до 50
    assert "...[обрезано]" in user_tight
    # При дефолтном лимите 2000 символов 100 символов не обрезаются
    assert "...[обрезано]" not in user_default
