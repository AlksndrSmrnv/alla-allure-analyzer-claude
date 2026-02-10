"""Тесты вспомогательной логики orchestrator для KB-поиска."""

from __future__ import annotations

from alla.models.clustering import ClusterSignature, FailureCluster
from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.orchestrator import _prepare_kb_log_query, _select_cluster_representative


def _failed_test(
    test_result_id: int,
    *,
    status_message: str | None = None,
    log_snippet: str | None = None,
) -> FailedTestSummary:
    return FailedTestSummary(
        test_result_id=test_result_id,
        name=f"test-{test_result_id}",
        status=Status.FAILED,
        status_message=status_message,
        log_snippet=log_snippet,
    )


def test_select_cluster_representative_matches_clustering_rule() -> None:
    """Берём тест с самым длинным message (при равенстве — с меньшим ID)."""
    tests = {
        100: _failed_test(100, status_message="short"),
        101: _failed_test(101, status_message="very long representative message"),
        102: _failed_test(102, status_message="mid"),
    }
    cluster = FailureCluster(
        cluster_id="c1",
        label="cluster",
        signature=ClusterSignature(),
        member_test_ids=[100, 101, 102],
        member_count=3,
    )

    representative = _select_cluster_representative(cluster, tests)

    assert representative is not None
    assert representative.test_result_id == 101


def test_prepare_kb_log_query_keeps_tail_on_truncation() -> None:
    """При обрезке сохраняется хвост, где обычно находится корневая ошибка."""
    log_snippet = "\n".join(
        [
            "INFO startup",
            "INFO request begin",
            "WARN retrying",
            "ERROR RootCauseException: boom",
        ]
    )

    prepared = _prepare_kb_log_query(log_snippet, max_chars=30)

    assert prepared is not None
    assert prepared.startswith("...[обрезано]")
    assert "RootCauseException" in prepared
    assert "startup" not in prepared
