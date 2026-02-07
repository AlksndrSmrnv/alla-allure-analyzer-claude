"""Тесты алгоритма message-first кластеризации."""

from __future__ import annotations

from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.services.clustering_service import ClusteringConfig, ClusteringService


def _failure(
    test_result_id: int,
    *,
    status_message: str | None = None,
    status_trace: str | None = None,
    category: str | None = None,
) -> FailedTestSummary:
    return FailedTestSummary(
        test_result_id=test_result_id,
        name=f"test-{test_result_id}",
        status=Status.FAILED,
        status_message=status_message,
        status_trace=status_trace,
        category=category,
    )


def _shared_trace() -> str:
    lines = [
        "at org.junit.jupiter.engine.execution.InvocationInterceptorChain.proceed",
        "at org.junit.jupiter.engine.execution.ExecutableInvoker.invoke",
        "at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0",
        "at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke",
    ]
    return "\n".join(lines) * 40


def test_different_messages_with_shared_trace_are_not_collapsed_at_high_threshold() -> None:
    trace = _shared_trace()
    failures = [
        _failure(
            1,
            status_message="AssertionError: expected [A] but found [B]",
            status_trace=f"ROOT_A\n{trace}",
        ),
        _failure(
            2,
            status_message="HTTP 401 Unauthorized from /api/profile",
            status_trace=f"ROOT_B\n{trace}",
        ),
        _failure(
            3,
            status_message="Database deadlock on table users",
            status_trace=f"ROOT_C\n{trace}",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    member_sets = sorted(tuple(cluster.member_test_ids) for cluster in report.clusters)
    assert report.cluster_count == 3
    assert member_sets == [(1,), (2,), (3,)]


def test_same_message_with_volatile_values_is_grouped_together() -> None:
    trace = "TimeoutException at com.acme.Client.call(Client.java:77)"
    failures = [
        _failure(
            10,
            status_message=(
                "Timeout waiting 5000 ms for job 123456 on host 10.1.2.3 "
                "at 2026-02-06 10:12:13"
            ),
            status_trace=trace,
        ),
        _failure(
            11,
            status_message=(
                "Timeout waiting 7000 ms for job 987654 on host 10.1.2.4 "
                "at 2026-02-06 10:12:14"
            ),
            status_trace=trace,
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].member_test_ids == [10, 11]


def test_message_only_errors_are_grouped_without_trace_penalty() -> None:
    failures = [
        _failure(15, status_message="AssertionError: expected status 200 got 500"),
        _failure(16, status_message="AssertionError: expected status 200 got 500"),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].member_test_ids == [15, 16]


def test_empty_messages_fallback_to_trace_and_split_when_trace_is_different() -> None:
    failures = [
        _failure(
            21,
            status_trace="SocketTimeoutException in HttpClient\nat net.client.Call.execute",
        ),
        _failure(
            22,
            status_trace="PSQLException deadlock detected\nat db.store.UserRepository.save",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    member_sets = sorted(tuple(cluster.member_test_ids) for cluster in report.clusters)
    assert report.cluster_count == 2
    assert member_sets == [(21,), (22,)]


def test_hyphenless_uuids_are_normalized() -> None:
    failures = [
        _failure(40, status_message="Failed for session a1b2c3d4e5f6789012345678abcdef90"),
        _failure(41, status_message="Failed for session ff00ff00ff00ff00ff00ff00ff00ff00"),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].member_test_ids == [40, 41]


def test_empty_messages_fallback_to_trace_and_merge_when_trace_is_similar() -> None:
    shared_tail = (
        "at net.client.Call.execute\n"
        "at net.client.Call.retry\n"
        "at net.client.Connection.send\n"
        "at net.client.Connection.await"
    )
    failures = [
        _failure(
            31,
            status_trace=(
                "SocketTimeoutException: timeout after 5000 request 123456\n"
                f"{shared_tail}\n{shared_tail}"
            ),
        ),
        _failure(
            32,
            status_trace=(
                "SocketTimeoutException: timeout after 7000 request 987654\n"
                f"{shared_tail}\n{shared_tail}"
            ),
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.9))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].member_test_ids == [31, 32]
