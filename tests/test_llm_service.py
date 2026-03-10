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
    _humanize_match_reason,
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
# _humanize_match_reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("matched_on", "expected_fragment"),
    [
        (
            ["Tier 1: exact substring match (score=1.00)"],
            "найден целиком",
        ),
        (
            ["Tier 2: line match (score=0.91)"],
            "Большинство строк",
        ),
        (
            ["Tier 3: TF-IDF similarity: 0.35 (example), 0.10 (title+desc), capped=0.38"],
            "Нечёткое текстовое совпадение",
        ),
        ([], "текстовое совпадение"),
    ],
)
def test_humanize_match_reason(matched_on: list[str], expected_fragment: str) -> None:
    """Tier-описания переводятся в понятные для LLM формулировки."""
    result = _humanize_match_reason(matched_on)
    assert expected_fragment in result


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
    """trace > 400 символов → обрезается с суффиксом '...[обрезано]'."""
    long_trace = "t" * 1200
    cluster = make_failure_cluster(example_trace_snippet=long_trace)
    prompt = build_cluster_prompt(cluster)

    assert "...[обрезано]" in prompt
    assert "t" * 401 not in prompt


def test_prompt_truncates_trace_from_head_only() -> None:
    """В prompt попадает только начало trace, без дальнего хвоста."""
    trace_text = "TRACE_HEAD\n" + ("t" * 500) + "\nTRACE_TAIL"
    cluster = make_failure_cluster(example_trace_snippet=trace_text)

    prompt = build_cluster_prompt(cluster)

    assert "TRACE_HEAD" in prompt
    assert "TRACE_TAIL" not in prompt
    assert "...[обрезано]" in prompt


def test_prompt_includes_log_snippet() -> None:
    """log_snippet передан → включён в промпт после мягкой нормализации."""
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, log_snippet="2024-01-01 [ERROR] boom")

    assert "<TS> [ERROR] boom" in prompt
    assert "2024-01-01 [ERROR] boom" not in prompt
    assert "Фрагмент лога" in prompt


def test_prompt_keeps_medium_log_snippet_without_truncation() -> None:
    """log_snippet до 8000 символов включается в prompt целиком."""
    cluster = make_failure_cluster()
    medium_log = "LOG_HEAD\n" + ("l" * 6000) + "\nLOG_TAIL"

    prompt = build_cluster_prompt(cluster, log_snippet=medium_log)

    assert medium_log in prompt
    assert "...[обрезано]" not in prompt


def test_prompt_truncates_long_log_after_8000_chars() -> None:
    """Очень длинный log_snippet режется только после нового лимита 8000."""
    cluster = make_failure_cluster()
    long_log = "LOG_HEAD\n" + ("l" * 9000) + "\nLOG_TAIL"

    prompt = build_cluster_prompt(cluster, log_snippet=long_log)

    assert "LOG_HEAD" in prompt
    assert "LOG_TAIL" not in prompt
    assert "l" * 8001 not in prompt
    assert "...[обрезано]" in prompt


def test_prompt_normalizes_trace_and_log_for_llm() -> None:
    """Trace и log проходят soft-normalization с <ID>/<TS>/<IP>, но без <NUM>."""
    cluster = make_failure_cluster(
        example_trace_snippet=(
            "trace uuid=123e4567-e89b-12d3-a456-426614174000 "
            "at 2026-02-06T10:12:13 from 10.20.30.40 build 123456"
        )
    )
    prompt = build_cluster_prompt(
        cluster,
        log_snippet=(
            "log uuid=123e4567e89b12d3a456426614174000 "
            "at 2026-02-06 10:12:13 from 10.20.30.40 build 987654"
        ),
    )

    assert "123e4567-e89b-12d3-a456-426614174000" not in prompt
    assert "123e4567e89b12d3a456426614174000" not in prompt
    assert "2026-02-06T10:12:13" not in prompt
    assert "2026-02-06 10:12:13" not in prompt
    assert "10.20.30.40" not in prompt
    assert "<ID>" in prompt
    assert "<TS>" in prompt
    assert "<IP>" in prompt
    assert "123456" in prompt
    assert "987654" in prompt
    assert "<NUM>" not in prompt


def test_prompt_keeps_example_message_raw_for_llm() -> None:
    """example_message не нормализуется для LLM."""
    raw_message = (
        "Order 123456 failed at 2026-02-06T10:12:13 "
        "for 123e4567-e89b-12d3-a456-426614174000 on 10.20.30.40"
    )
    cluster = make_failure_cluster(example_message=raw_message)

    prompt = build_cluster_prompt(cluster)

    assert raw_message in prompt
    assert "<ID>" not in prompt
    assert "<TS>" not in prompt
    assert "<IP>" not in prompt


def test_prompt_normalizes_full_trace_for_llm() -> None:
    """Если передан full_trace, в prompt уходит его мягко нормализованная версия."""
    cluster = make_failure_cluster(example_trace_snippet="fallback trace")
    full_trace = (
        "Full trace for 123e4567-e89b-12d3-a456-426614174000 "
        "at 2026-02-06T10:12:13 from 10.20.30.40"
    )

    prompt = build_cluster_prompt(cluster, full_trace=full_trace)

    assert "fallback trace" not in prompt
    assert "123e4567-e89b-12d3-a456-426614174000" not in prompt
    assert "2026-02-06T10:12:13" not in prompt
    assert "10.20.30.40" not in prompt
    assert "Full trace for <ID> at <TS> from <IP>" in prompt


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


def test_prompt_prioritizes_top_kb_match() -> None:
    """Промпт явно заставляет LLM начинать анализ с лучшего KB-совпадения."""
    entry = make_kb_entry(title="DNS failure", category=RootCauseCategory.ENV)
    match = make_kb_match_result(entry=entry, score=0.91)
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "БАЗА ЗНАНИЙ — основной источник интерпретации" in prompt
    assert "Сначала смотри KB #1" in prompt
    assert "Если у KB #1 score >= 0.70" in prompt
    assert "KB #1 [основная гипотеза; высокое совпадение; score 0.91]" in prompt


def test_prompt_kb_category_is_translated_for_llm() -> None:
    """KB category показывается в терминологии, которую должна вернуть LLM."""
    entry = make_kb_entry(title="Service outage", category=RootCauseCategory.SERVICE)
    match = make_kb_match_result(entry=entry, score=0.75)
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Категория: приложение" in prompt
    assert "Категория: service" not in prompt


def test_prompt_kb_matches_include_match_reason() -> None:
    """Промпт показывает понятное объяснение KB-совпадения вместо технического tier."""
    match = make_kb_match_result(
        score=0.75,
        matched_on=["Tier 1: exact substring match (score=0.75)"],
    )
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Почему похоже: " in prompt
    assert "найден целиком" in prompt
    assert "Tier 1: exact substring match" not in prompt


def test_prompt_describes_match_against_cluster_data() -> None:
    """Tier-совпадение описывается через данные кластера, а не только текст ошибки."""
    match = make_kb_match_result(
        score=1.0,
        matched_on=["Tier 1: exact substring match (score=1.00)"],
    )
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Пример ошибки из KB найден целиком в данных кластера" in prompt
    assert "тексте ошибки кластера" not in prompt


def test_prompt_includes_error_example_for_top_kb_matches() -> None:
    """error_example из KB включается в промпт для первых 2 совпадений."""
    entry1 = make_kb_entry(
        id="e1",
        title="DNS Failure",
        error_example="ERROR: DNS resolution failed for service-x.namespace.svc.cluster.local",
    )
    entry2 = make_kb_entry(
        id="e2",
        title="Timeout",
        error_example="Connection timed out after 30000ms",
    )
    entry3 = make_kb_entry(
        id="e3",
        title="OOM",
        error_example="java.lang.OutOfMemoryError: Java heap space",
    )
    matches = [
        make_kb_match_result(entry=entry1, score=0.91),
        make_kb_match_result(entry=entry2, score=0.75),
        make_kb_match_result(entry=entry3, score=0.50),
    ]
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=matches)

    # First 2 KB matches include error_example
    assert "DNS resolution failed for service-x" in prompt
    assert "Connection timed out after 30000ms" in prompt
    # KB #3 does NOT include error_example
    assert "OutOfMemoryError" not in prompt
    assert "Пример ошибки из KB (с чем сравнивалось)" in prompt


def test_prompt_truncates_long_error_example() -> None:
    """error_example > 500 символов обрезается."""
    entry = make_kb_entry(error_example="e" * 800)
    match = make_kb_match_result(entry=entry, score=0.91)
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "e" * 501 not in prompt
    assert "Пример ошибки из KB (с чем сравнивалось)" in prompt


def test_prompt_includes_verification_framing_for_high_score_kb() -> None:
    """score >= 0.70 (не exact) добавляет инструкцию по проверке KB."""
    match = make_kb_match_result(
        score=0.85,
        matched_on=["Tier 2: line match (score=0.85)"],
    )
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Инструкция по проверке KB #1" in prompt
    assert "НЕ является противоречием" in prompt
    assert "ПРЯМО указывают на другую причину" in prompt


def test_prompt_includes_provenance_for_combined_kb_query() -> None:
    """Промпт объясняет, что KB найдено по объединённым message/trace/log данным."""
    match = make_kb_match_result(score=0.91)
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(
        cluster,
        kb_matches=[match],
        kb_query_provenance=(120, 340, 560),
    )

    assert "KB-совпадение найдено по объединённому тексту" in prompt
    assert "сообщение об ошибке (120 симв.)" in prompt
    assert "стек-трейс (340 симв.)" in prompt
    assert "лог приложения (560 симв.)" in prompt
    assert "совпадение могло быть именно по ним" in prompt


def test_prompt_includes_verification_framing_for_exact_high_score_kb() -> None:
    """Даже exact/high-score KB отправляется на проверку по данным кластера."""
    match = make_kb_match_result(
        score=1.0,
        matched_on=["Tier 1: exact substring match (score=1.00)"],
    )
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "EXACT MATCH: KB #1 имеет score 1.00" in prompt
    assert "Инструкция по проверке KB #1" in prompt
    assert "Сообщение об ошибке тест-фреймворка (assertion) часто отличается" in prompt


def test_prompt_no_verification_framing_for_low_score_kb() -> None:
    """score < 0.70 не добавляет инструкцию по проверке."""
    match = make_kb_match_result(
        score=0.45,
        matched_on=["Tier 3: TF-IDF similarity: 0.45 (example)"],
    )
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "Инструкция по проверке KB #1" not in prompt


def test_prompt_decision_rules_require_citation_to_override_kb() -> None:
    """Правила принятия решения требуют цитату для отклонения KB #1."""
    match = make_kb_match_result(score=0.91)
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "ОБЯЗАН процитировать" in prompt
    assert "Различие в формулировке" in prompt


def test_prompt_marks_exact_kb_match_as_mandatory() -> None:
    """Exact KB match помечается как обязательная основная причина."""
    match = make_kb_match_result(
        score=1.0,
        matched_on=["Tier 1: exact substring match (score=1.00)"],
    )
    cluster = make_failure_cluster()

    prompt = build_cluster_prompt(cluster, kb_matches=[match])

    assert "EXACT MATCH: KB #1 имеет score 1.00" in prompt
    assert "KB #2 и KB #3 игнорируй" in prompt
    assert "KB #1 [exact match; обязательная основная причина;" in prompt


def test_prompt_without_kb_has_continuous_rule_numbering() -> None:
    """Без KB правила в секции решения нумеруются подряд, без пропусков."""
    cluster = make_failure_cluster()
    prompt = build_cluster_prompt(cluster, kb_matches=None)

    rules = prompt.split("ПРАВИЛА ПРИНЯТИЯ РЕШЕНИЯ:\n", 1)[1]

    assert "1. Базы знаний нет" in rules
    assert "2. Каждый шаг должен быть привязан" in rules
    assert "3. Не давай абстрактных советов" in rules
    assert "4. Не выдумывай новые причины" in rules
    assert "\n5. " not in rules


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
async def test_analyze_clusters_sends_exact_kb_match_to_langflow() -> None:
    """Даже Tier 1 exact KB match идёт в Langflow для дополнительной проверки."""
    captured_prompts: list[str] = []

    class _Client:
        async def run_flow(self, input_value):
            captured_prompts.append(input_value)
            return "validated by langflow"

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster()
    report = _make_report(cluster)
    kb_match = make_kb_match_result(
        entry=make_kb_entry(
            title="DNS failure",
            description="Сервис не резолвится по DNS",
            category=RootCauseCategory.ENV,
            resolution_steps=["Check DNS servers", "Restart CoreDNS"],
        ),
        score=1.0,
        matched_on=["Tier 1: exact substring match (score=1.00)"],
    )

    result = await service.analyze_clusters(
        report,
        kb_results={"c1": [kb_match]},
    )

    assert result.analyzed_count == 1
    assert result.failed_count == 0
    assert result.kb_bypass_count == 0
    assert len(captured_prompts) == 1
    assert "EXACT MATCH: KB #1 имеет score 1.00" in captured_prompts[0]
    assert "Инструкция по проверке KB #1" in captured_prompts[0]
    analysis = result.cluster_analyses["c1"]
    assert analysis.analysis_text == "validated by langflow"
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
async def test_analyze_clusters_includes_kb_provenance_when_message_differs() -> None:
    """При непохожем message промпт явно отправляет LLM перепроверять trace/log."""
    captured_prompts: list[str] = []

    class _Client:
        async def run_flow(self, input_value):
            captured_prompts.append(input_value)
            return "analysis"

    service = LLMService(_Client())  # type: ignore[arg-type]
    cluster = make_failure_cluster(
        example_message="AssertionError: expected 200 but got 500",
        example_trace_snippet="at ApiTest.java:42",
        representative_test_id=42,
        member_test_ids=[42],
    )
    report = _make_report(cluster)
    kb_match = make_kb_match_result(
        entry=make_kb_entry(
            title="DNS failure",
            error_example="ERROR: DNS resolution failed for service-x.namespace.svc.cluster.local",
            category=RootCauseCategory.ENV,
        ),
        score=0.91,
        matched_on=["Tier 2: line match (score=0.91)"],
    )
    failed_tests = [
        FailedTestSummary(
            test_result_id=42,
            name="test",
            status=TestStatus.FAILED,
            log_snippet="ERROR: DNS resolution failed for service-x.namespace.svc.cluster.local",
            status_trace="java.net.UnknownHostException: service-x.namespace.svc.cluster.local",
        ),
    ]

    await service.analyze_clusters(
        report,
        kb_results={"c1": [kb_match]},
        failed_tests=failed_tests,
        kb_provenance={"c1": (34, 78, 96)},
    )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "KB-совпадение найдено по объединённому тексту" in prompt
    assert "сообщение об ошибке (34 симв.)" in prompt
    assert "стек-трейс (78 симв.)" in prompt
    assert "лог приложения (96 симв.)" in prompt
    assert "Если сообщение об ошибке не похоже на пример из KB" in prompt
    assert "проверь стек-трейс и лог" in prompt
    assert "AssertionError: expected 200 but got 500" in prompt
    assert "DNS resolution failed for service-x.namespace.svc.cluster.local" in prompt


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
