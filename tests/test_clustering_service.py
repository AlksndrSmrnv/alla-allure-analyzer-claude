"""Тесты алгоритма message-first кластеризации."""

from __future__ import annotations

from alla.models.common import TestStatus as Status
from alla.models.testops import FailedTestSummary
from alla.services.clustering_service import (
    ClusteringConfig,
    ClusteringService,
    _normalize_text,
)


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


# ---------------------------------------------------------------------------
# Unit-тесты _normalize_text — нормализация дат и времени
# ---------------------------------------------------------------------------


class TestNormalizeDateFormats:
    """Все форматы дат/времени должны заменяться на <TS>."""

    # --- ISO 8601 полный datetime ---

    def test_iso_datetime_basic(self) -> None:
        assert _normalize_text("error at 2026-02-06T10:12:13") == "error at <TS>"

    def test_iso_datetime_space_separator(self) -> None:
        assert _normalize_text("error at 2026-02-06 10:12:13") == "error at <TS>"

    def test_iso_datetime_millis(self) -> None:
        assert _normalize_text("error at 2026-02-06T10:12:13.123") == "error at <TS>"

    def test_iso_datetime_micros(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13.123456") == "at <TS>"

    def test_iso_datetime_utc_z(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13Z") == "at <TS>"

    def test_iso_datetime_tz_with_colon(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13+03:00") == "at <TS>"

    def test_iso_datetime_tz_without_colon(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13+0300") == "at <TS>"

    def test_iso_datetime_millis_and_tz(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13.123+03:00 fail") == "at <TS> fail"

    def test_iso_datetime_negative_tz(self) -> None:
        assert _normalize_text("at 2026-02-06T10:12:13-05:00") == "at <TS>"

    # --- Java / Log4j запятая перед миллисекундами ---

    def test_java_log4j_comma_millis(self) -> None:
        assert _normalize_text("2026-02-06 10:12:13,123 ERROR") == "<TS> ERROR"

    # --- ISO дата без времени ---

    def test_date_only_iso(self) -> None:
        assert _normalize_text("report for 2026-02-06 generated") == "report for <TS> generated"

    def test_date_only_iso_single_replacement(self) -> None:
        """Полный datetime → один <TS>, а не дата + время отдельно."""
        result = _normalize_text("at 2026-02-06T10:12:13 done")
        assert result == "at <TS> done"
        assert result.count("<TS>") == 1

    # --- Слэш-даты ---

    def test_slash_date_mdy(self) -> None:
        assert _normalize_text("date: 02/06/2026") == "date: <TS>"

    def test_slash_date_ymd(self) -> None:
        assert _normalize_text("date: 2026/02/06") == "date: <TS>"

    def test_slash_date_dmy(self) -> None:
        assert _normalize_text("date: 6/2/2026") == "date: <TS>"

    # --- Точка-даты ---

    def test_dot_date_dmy(self) -> None:
        assert _normalize_text("дата: 06.02.2026") == "дата: <TS>"

    def test_dot_date_ymd(self) -> None:
        assert _normalize_text("date: 2026.02.06") == "date: <TS>"

    # --- Именованные месяцы ---

    def test_named_month_mon_dd_yyyy(self) -> None:
        assert _normalize_text("on Feb 6, 2026 failed") == "on <TS> failed"

    def test_named_month_dd_mon_yyyy(self) -> None:
        assert _normalize_text("on 06 Feb 2026 failed") == "on <TS> failed"

    def test_named_month_full_name(self) -> None:
        assert _normalize_text("on February 6, 2026 failed") == "on <TS> failed"

    def test_named_month_hyphenated(self) -> None:
        assert _normalize_text("on 6-Feb-2026 failed") == "on <TS> failed"

    def test_named_month_with_time(self) -> None:
        assert _normalize_text("on Feb 6, 2026 10:12:13 failed") == "on <TS> failed"

    def test_named_month_december(self) -> None:
        assert _normalize_text("on 25 December 2025 error") == "on <TS> error"

    # --- Standalone время ---

    def test_time_only(self) -> None:
        assert _normalize_text("at 10:12:13 the error") == "at <TS> the error"

    def test_time_only_with_millis(self) -> None:
        assert _normalize_text("at 10:12:13.123 error") == "at <TS> error"

    def test_time_only_with_comma_millis(self) -> None:
        assert _normalize_text("at 10:12:13,456 error") == "at <TS> error"

    # --- Защита от ложных срабатываний ---

    def test_ip_not_matched_as_dot_date(self) -> None:
        assert _normalize_text("host 192.168.1.1 failed") == "host <IP> failed"

    def test_http_status_codes_preserved(self) -> None:
        assert _normalize_text("HTTP 200 OK") == "HTTP 200 OK"
        assert _normalize_text("got 404 not found") == "got 404 not found"

    def test_short_numbers_preserved(self) -> None:
        assert _normalize_text("line 42 col 7") == "line 42 col 7"

    def test_version_three_segments_short(self) -> None:
        """Версии вида 4.15.0 (последний сегмент < 2 цифр) не должны матчиться."""
        assert _normalize_text("selenium 4.15.0 error") == "selenium 4.15.0 error"

    def test_version_two_segments(self) -> None:
        assert _normalize_text("version 1.2.3") == "version 1.2.3"

    def test_multiple_formats_in_one_string(self) -> None:
        text = "started 2026-02-06T10:12:13Z on host 10.1.2.3 job 123456"
        result = _normalize_text(text)
        assert "<TS>" in result
        assert "<IP>" in result
        assert "<NUM>" in result

    def test_uuid_before_dates(self) -> None:
        text = "id=a1b2c3d4-e5f6-7890-abcd-ef1234567890 at 2026-02-06"
        result = _normalize_text(text)
        assert "<ID>" in result
        assert "<TS>" in result


# ---------------------------------------------------------------------------
# Интеграционный тест: кластеризация ошибок с разными форматами дат
# ---------------------------------------------------------------------------


def test_same_message_with_various_date_formats_is_grouped() -> None:
    """Ошибки, отличающиеся только форматом даты, должны попасть в один кластер."""
    failures = [
        _failure(
            50,
            status_message="Report generation failed for date 2026-02-06T10:12:13.123Z",
        ),
        _failure(
            51,
            status_message="Report generation failed for date 02/06/2026",
        ),
        _failure(
            52,
            status_message="Report generation failed for date Feb 6, 2026 10:12:13",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [50, 51, 52]
