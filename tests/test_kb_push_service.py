"""Тесты KBPushService: форматирование комментариев, дедупликация, параллельный push."""

from __future__ import annotations

import pytest

from alla.knowledge.models import RootCauseCategory
from alla.models.clustering import ClusteringReport
from alla.models.common import TestStatus
from alla.models.testops import FailedTestSummary, TriageReport
from alla.services.kb_push_service import KBPushService, format_kb_description
from conftest import make_failure_cluster, make_kb_entry, make_kb_match_result


# ---------------------------------------------------------------------------
# format_kb_description
# ---------------------------------------------------------------------------


def test_format_kb_description_empty_list() -> None:
    """matches=[] → пустая строка."""
    assert format_kb_description([]) == ""


def test_format_kb_description_includes_title_and_category() -> None:
    """Комментарий содержит title и категорию KB-записи."""
    entry = make_kb_entry(title="DNS failure", category=RootCauseCategory.ENV)
    match = make_kb_match_result(entry=entry)
    result = format_kb_description([match])

    assert "DNS failure" in result
    assert "env" in result
    assert "[alla] Рекомендация" in result


def test_format_kb_description_numbered_steps() -> None:
    """Шаги по устранению пронумерованы."""
    entry = make_kb_entry(resolution_steps=["Step A", "Step B", "Step C"])
    match = make_kb_match_result(entry=entry)
    result = format_kb_description([match])

    assert "1. Step A" in result
    assert "2. Step B" in result
    assert "3. Step C" in result


def test_format_kb_description_multiple_matches_separated() -> None:
    """Несколько matches → разделены дефисами."""
    m1 = make_kb_match_result(entry=make_kb_entry(id="a", title="First"))
    m2 = make_kb_match_result(entry=make_kb_entry(id="b", title="Second"))
    result = format_kb_description([m1, m2])

    assert "First" in result
    assert "Second" in result
    assert "-" * 40 in result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_push_data(
    cluster_tests: dict[str, list[tuple[int, int | None]]],
    kb_cluster_ids: set[str] | None = None,
) -> tuple[ClusteringReport, dict, TriageReport]:
    """Собрать тестовые данные для push_kb_results."""
    clusters = []
    failed_tests = []
    kb_results = {}

    for cid, tests in cluster_tests.items():
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

        if kb_cluster_ids is None or cid in kb_cluster_ids:
            kb_results[cid] = [make_kb_match_result()]

    report = ClusteringReport(
        launch_id=1,
        total_failures=sum(len(t) for t in cluster_tests.values()),
        cluster_count=len(clusters),
        clusters=clusters,
    )
    triage = TriageReport(
        launch_id=1,
        total_results=100,
        failed_tests=failed_tests,
    )
    return report, kb_results, triage


# ---------------------------------------------------------------------------
# push_kb_results — дедупликация
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_deduplicates_by_test_case_id() -> None:
    """2 теста с одним test_case_id → 1 вызов post_comment."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, kb_results, triage = _make_push_data({"c1": [(1, 100), (2, 100)]})
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    result = await service.push_kb_results(report, kb_results, triage)

    assert len(posted) == 1
    assert result.updated_count == 1
    assert result.skipped_count >= 1


@pytest.mark.asyncio
async def test_push_skips_test_without_test_case_id() -> None:
    """test_case_id=None → тест пропущен."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, kb_results, triage = _make_push_data({"c1": [(1, None), (2, 200)]})
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    result = await service.push_kb_results(report, kb_results, triage)

    assert len(posted) == 1
    assert posted[0] == 200
    assert result.skipped_count >= 1


@pytest.mark.asyncio
async def test_push_skips_cluster_without_kb_matches() -> None:
    """Кластер без KB-совпадений → все его тесты пропущены."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report, kb_results, triage = _make_push_data(
        {"c1": [(1, 100)], "c2": [(2, 200)]},
        kb_cluster_ids={"c1"},
    )
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    result = await service.push_kb_results(report, kb_results, triage)

    assert len(posted) == 1
    assert posted[0] == 100


@pytest.mark.asyncio
async def test_push_first_cluster_wins_for_same_tc_id() -> None:
    """tc_id в 2 кластерах → комментарий от первого кластера."""
    posted_bodies: list[str] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted_bodies.append(body)

    c1 = make_failure_cluster(cluster_id="c1", member_test_ids=[1])
    c2 = make_failure_cluster(cluster_id="c2", member_test_ids=[2])

    triage = TriageReport(
        launch_id=1,
        total_results=10,
        failed_tests=[
            FailedTestSummary(test_result_id=1, name="t1", status=TestStatus.FAILED, test_case_id=100),
            FailedTestSummary(test_result_id=2, name="t2", status=TestStatus.FAILED, test_case_id=100),
        ],
    )
    report = ClusteringReport(
        launch_id=1, total_failures=2, cluster_count=2, clusters=[c1, c2],
    )
    kb_results = {
        "c1": [make_kb_match_result(entry=make_kb_entry(title="First KB"))],
        "c2": [make_kb_match_result(entry=make_kb_entry(title="Second KB"))],
    }

    service = KBPushService(_Updater())  # type: ignore[arg-type]
    await service.push_kb_results(report, kb_results, triage)

    assert len(posted_bodies) == 1
    assert "First KB" in posted_bodies[0]


# ---------------------------------------------------------------------------
# push_kb_results — error resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_continues_on_single_error() -> None:
    """post_comment raises для одного теста → остальные обновляются."""

    class _Updater:
        async def post_comment(self, tc_id, body):
            if tc_id == 100:
                raise RuntimeError("API error")

    report, kb_results, triage = _make_push_data(
        {"c1": [(1, 100), (2, 200), (3, 300)]},
    )
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    result = await service.push_kb_results(report, kb_results, triage)

    assert result.failed_count == 1
    assert result.updated_count == 2


# ---------------------------------------------------------------------------
# push_kb_results — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_empty_clusters() -> None:
    """Пустой clustering_report → нет вызовов post_comment."""
    posted: list[int] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted.append(tc_id)

    report = ClusteringReport(
        launch_id=1, total_failures=0, cluster_count=0, clusters=[],
    )
    triage = TriageReport(launch_id=1, total_results=0)
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    result = await service.push_kb_results(report, {}, triage)

    assert len(posted) == 0
    assert result.updated_count == 0


@pytest.mark.asyncio
async def test_push_comment_starts_with_alla_prefix() -> None:
    """Комментарий начинается с '[alla] Рекомендация'."""
    posted_bodies: list[str] = []

    class _Updater:
        async def post_comment(self, tc_id, body):
            posted_bodies.append(body)

    report, kb_results, triage = _make_push_data({"c1": [(1, 100)]})
    service = KBPushService(_Updater())  # type: ignore[arg-type]

    await service.push_kb_results(report, kb_results, triage)

    assert len(posted_bodies) == 1
    assert posted_bodies[0].startswith("[alla] Рекомендация")
