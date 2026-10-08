"""Тесты алгоритма message-first кластеризации."""

from __future__ import annotations

import pytest

from skill_fixtures import without_libmagic  # noqa: F401
from skill_factories import make_error_log, make_failed_test_summary
from alla_core.models.common import TestStatus as Status
from alla_core.models.testops import FailedTestSummary
from alla_core.services.clustering_service import (
    ClusteringConfig,
    ClusteringService,
    _LogErrorGate,
    _log_error_blocks,
    _extract_assertion_actual,
    _strip_correlation_only_http_sections,
)
from alla_core.utils.message_resources import message_resources
from alla_core.utils.text_normalization import normalize_text


def _failure(
    test_result_id: int,
    *,
    status_message: str | None = None,
    status_trace: str | None = None,
    category: str | None = None,
    log_snippet: str | None = None,
    correlation_hint: str | None = None,
    failed_step_path: str | None = None,
) -> FailedTestSummary:
    return FailedTestSummary(
        test_result_id=test_result_id,
        name=f"test-{test_result_id}",
        status=Status.FAILED,
        status_message=status_message,
        status_trace=status_trace,
        category=category,
        log_snippet=log_snippet,
        correlation_hint=correlation_hint,
        failed_step_path=failed_step_path,
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
# Unit-тесты normalize_text — нормализация дат и времени
# ---------------------------------------------------------------------------


class TestNormalizeDateFormats:
    """Все форматы дат/времени должны заменяться на <TS>."""

    # --- ISO 8601 полный datetime ---

    def test_iso_datetime_basic(self) -> None:
        assert normalize_text("error at 2026-02-06T10:12:13") == "error at <TS>"

    def test_iso_datetime_space_separator(self) -> None:
        assert normalize_text("error at 2026-02-06 10:12:13") == "error at <TS>"

    def test_iso_datetime_millis(self) -> None:
        assert normalize_text("error at 2026-02-06T10:12:13.123") == "error at <TS>"

    def test_iso_datetime_micros(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13.123456") == "at <TS>"

    def test_iso_datetime_utc_z(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13Z") == "at <TS>"

    def test_iso_datetime_tz_with_colon(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13+03:00") == "at <TS>"

    def test_iso_datetime_tz_without_colon(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13+0300") == "at <TS>"

    def test_iso_datetime_millis_and_tz(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13.123+03:00 fail") == "at <TS> fail"

    def test_iso_datetime_negative_tz(self) -> None:
        assert normalize_text("at 2026-02-06T10:12:13-05:00") == "at <TS>"

    # --- ISO 8601 datetime без секунд (HH:MM) ---

    def test_iso_datetime_hhmm_t_separator(self) -> None:
        assert normalize_text("at 2026-02-06T10:12 done") == "at <TS> done"

    def test_iso_datetime_hhmm_space_separator(self) -> None:
        assert normalize_text("error at 2026-02-06 10:12 done") == "error at <TS> done"

    def test_iso_datetime_hhmm_with_tz(self) -> None:
        assert normalize_text("at 2026-02-06T10:12Z end") == "at <TS> end"

    def test_iso_datetime_hhmm_single_replacement(self) -> None:
        """HH:MM datetime → один <TS>, а не дата + остаток."""
        result = normalize_text("at 2026-02-06 10:12 done")
        assert result.count("<TS>") == 1

    # --- Java / Log4j запятая перед миллисекундами ---

    def test_java_log4j_comma_millis(self) -> None:
        assert normalize_text("2026-02-06 10:12:13,123 ERROR") == "<TS> ERROR"

    # --- ISO дата без времени ---

    def test_date_only_iso(self) -> None:
        assert normalize_text("report for 2026-02-06 generated") == "report for <TS> generated"

    def test_date_only_iso_single_replacement(self) -> None:
        """Полный datetime → один <TS>, а не дата + время отдельно."""
        result = normalize_text("at 2026-02-06T10:12:13 done")
        assert result == "at <TS> done"
        assert result.count("<TS>") == 1

    def test_date_only_iso_followed_by_space_and_digit(self) -> None:
        """Дата + пробел + цифра (не время) — дата должна нормализоваться."""
        assert (
            normalize_text("error on 2026-02-06 2 retries left")
            == "error on <TS> 2 retries left"
        )

    # --- Слэш-даты ---

    def test_slash_date_mdy(self) -> None:
        assert normalize_text("date: 02/06/2026") == "date: <TS>"

    def test_slash_date_ymd(self) -> None:
        assert normalize_text("date: 2026/02/06") == "date: <TS>"

    def test_slash_date_dmy(self) -> None:
        assert normalize_text("date: 6/2/2026") == "date: <TS>"

    # --- Точка-даты ---

    def test_dot_date_dmy(self) -> None:
        assert normalize_text("дата: 06.02.2026") == "дата: <TS>"

    def test_dot_date_ymd(self) -> None:
        assert normalize_text("date: 2026.02.06") == "date: <TS>"

    # --- Именованные месяцы ---

    def test_named_month_mon_dd_yyyy(self) -> None:
        assert normalize_text("on Feb 6, 2026 failed") == "on <TS> failed"

    def test_named_month_dd_mon_yyyy(self) -> None:
        assert normalize_text("on 06 Feb 2026 failed") == "on <TS> failed"

    def test_named_month_full_name(self) -> None:
        assert normalize_text("on February 6, 2026 failed") == "on <TS> failed"

    def test_named_month_hyphenated(self) -> None:
        assert normalize_text("on 6-Feb-2026 failed") == "on <TS> failed"

    def test_named_month_with_time(self) -> None:
        assert normalize_text("on Feb 6, 2026 10:12:13 failed") == "on <TS> failed"

    def test_named_month_december(self) -> None:
        assert normalize_text("on 25 December 2025 error") == "on <TS> error"

    # --- Standalone время ---

    def test_time_only(self) -> None:
        assert normalize_text("at 10:12:13 the error") == "at <TS> the error"

    def test_time_only_with_millis(self) -> None:
        assert normalize_text("at 10:12:13.123 error") == "at <TS> error"

    def test_time_only_with_comma_millis(self) -> None:
        assert normalize_text("at 10:12:13,456 error") == "at <TS> error"

    # --- Защита от ложных срабатываний ---

    def test_ip_not_matched_as_dot_date(self) -> None:
        assert normalize_text("host 192.168.1.1 failed") == "host <IP> failed"

    def test_http_status_codes_preserved(self) -> None:
        assert normalize_text("HTTP 200 OK") == "HTTP 200 OK"
        assert normalize_text("got 404 not found") == "got 404 not found"

    def test_short_numbers_preserved(self) -> None:
        assert normalize_text("line 42 col 7") == "line 42 col 7"

    def test_version_three_segments_short(self) -> None:
        """Версии вида 4.15.0 (последний сегмент < 2 цифр) не должны матчиться."""
        assert normalize_text("selenium 4.15.0 error") == "selenium 4.15.0 error"

    def test_version_two_segments(self) -> None:
        assert normalize_text("version 1.2.3") == "version 1.2.3"

    def test_multiple_formats_in_one_string(self) -> None:
        text = "started 2026-02-06T10:12:13Z on host 10.1.2.3 job 123456"
        result = normalize_text(text)
        assert "<TS>" in result
        assert "<IP>" in result
        assert "<NUM>" in result

    def test_uuid_before_dates(self) -> None:
        text = "id=a1b2c3d4-e5f6-7890-abcd-ef1234567890 at 2026-02-06"
        result = normalize_text(text)
        assert "<ID>" in result
        assert "<TS>" in result


class TestNormalizeAssertionValues:
    """Длинные числа в assertion-форматах сохраняются для специфичности KB."""

    def test_long_number_in_double_quotes_preserved(self) -> None:
        assert normalize_text('was "-1001"') == 'was "-1001"'

    def test_long_number_in_single_quotes_preserved(self) -> None:
        assert normalize_text("was '1001'") == "was '1001'"

    def test_long_number_in_angle_brackets_preserved(self) -> None:
        assert normalize_text("but was: <12345>") == "but was: <12345>"

    def test_long_number_in_square_brackets_preserved(self) -> None:
        assert normalize_text("but was [123456]") == "but was [123456]"

    def test_full_hamcrest_message_preserved(self) -> None:
        msg = 'Expected: "-2206" but: was "-1001"'
        assert normalize_text(msg) == msg

    def test_negative_long_number_in_angle_brackets_preserved(self) -> None:
        assert (
            normalize_text("expected: <-2206> but was: <-1001>")
            == "expected: <-2206> but was: <-1001>"
        )

    def test_unquoted_long_number_still_replaced(self) -> None:
        assert normalize_text("job 123456") == "job <NUM>"

    def test_mixed_quoted_and_unquoted(self) -> None:
        # "1234" защищено; 567890 — нет.
        assert normalize_text('id="1234" count 567890') == 'id="1234" count <NUM>'

    def test_asymmetric_delimiter_also_protects(self) -> None:
        # OR-семантика: достаточно одного delimiter-символа с любой стороны.
        assert normalize_text("value <1001 end") == "value <1001 end"


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


def test_clustering_config_exposes_step_path_penalty_fields() -> None:
    """ClusteringConfig содержит поля step_path_mismatch_penalty и step_path_log_reduction."""
    config = ClusteringConfig()
    assert config.step_path_mismatch_penalty == 0.45
    assert config.step_path_log_reduction == 0.5

    custom = ClusteringConfig(step_path_mismatch_penalty=0.3, step_path_log_reduction=0.4)
    assert custom.step_path_mismatch_penalty == 0.3
    assert custom.step_path_log_reduction == 0.4


def test_clustering_config_exposes_step_path_strict_threshold() -> None:
    """ClusteringConfig содержит step_path_strict_threshold с дефолтом 0.95."""
    assert ClusteringConfig().step_path_strict_threshold == 0.95
    assert ClusteringConfig(step_path_strict_threshold=0.5).step_path_strict_threshold == 0.5


# ---------------------------------------------------------------------------
# Unit-тесты _extract_assertion_actual
# ---------------------------------------------------------------------------


class TestExtractAssertionActual:
    """Извлечение actual-значения из assertion-паттернов."""

    def test_angle_brackets(self) -> None:
        assert _extract_assertion_actual("expected: <0> but was: <33>") == "33"

    def test_square_brackets(self) -> None:
        assert _extract_assertion_actual("expected [200] but was [404]") == "404"

    def test_quotes(self) -> None:
        assert _extract_assertion_actual('expected "OK" but was "ERROR"') == "ERROR"

    def test_russian_variant(self) -> None:
        assert _extract_assertion_actual("ожидалось: <0> но было: <1>") == "1"

    def test_no_match(self) -> None:
        assert _extract_assertion_actual("NullPointerException at line 42") is None

    def test_empty_string(self) -> None:
        assert _extract_assertion_actual("") is None

    def test_string_status_code(self) -> None:
        assert _extract_assertion_actual("expected: <SUCCESS> but was: <FAIL>") == "FAIL"

    def test_whitespace_normalized(self) -> None:
        """Пробелы внутри delimiters не влияют на сравнение: '< 33 >' == '<33>'."""
        assert _extract_assertion_actual("but was: < 33 >") == "33"
        assert _extract_assertion_actual("but was: <33>") == "33"

    def test_inner_tabs_and_newlines_collapsed(self) -> None:
        assert _extract_assertion_actual("but was: < some\t value >") == "some value"

    def test_actual_line(self) -> None:
        assert _extract_assertion_actual("Expected: 0\nActual: 42") == "42"

    def test_russian_actual_line(self) -> None:
        assert _extract_assertion_actual("Ожидаемое: ok\nФактическое: server-error") == "server-error"

    def test_actual_word_in_free_text_does_not_match(self) -> None:
        assert _extract_assertion_actual("The actual results were inconsistent") is None

    def test_actual_after_log_prefix_does_not_match(self) -> None:
        assert _extract_assertion_actual("foo\n[INFO] Actual: 42 received") is None


# ---------------------------------------------------------------------------
# Unit-тесты _strip_correlation_only_http_sections
# ---------------------------------------------------------------------------


class TestStripCorrelationOnlyHttpSections:
    """Фильтрация HTTP-секций с только корреляционными ID."""

    def test_correlation_only_removed(self) -> None:
        snippet = (
            "--- [HTTP: Отправлен запрос -> ] ---\n"
            "Корреляция: operUID=qwe123, rquid=rty456"
        )
        assert _strip_correlation_only_http_sections(snippet) == ""

    def test_http_section_with_error_preserved(self) -> None:
        snippet = (
            "--- [HTTP: Ответ сервера] ---\n"
            "Корреляция: operUID=abc, rquid=def\n"
            "HTTP статус: 500\n"
            "errorMessage: Internal Server Error"
        )
        result = _strip_correlation_only_http_sections(snippet)
        assert "HTTP статус: 500" in result
        assert "errorMessage:" in result

    def test_no_section_headers_passthrough(self) -> None:
        plain = "some log line\nanother line"
        assert _strip_correlation_only_http_sections(plain) == plain

    def test_mixed_file_and_correlation_only_http(self) -> None:
        snippet = (
            "--- [файл: app.log] ---\n"
            "2026-01-01T10:00:00 [ERROR] NullPointerException\n\n"
            "--- [HTTP: Запрос] ---\n"
            "Корреляция: operUID=aaa, rquid=bbb"
        )
        result = _strip_correlation_only_http_sections(snippet)
        assert "[файл: app.log]" in result
        assert "NullPointerException" in result
        assert "Корреляция:" not in result

    def test_all_correlation_only_returns_empty(self) -> None:
        snippet = (
            "--- [HTTP: Запрос 1] ---\n"
            "Корреляция: operUID=a1, rquid=b1\n\n"
            "--- [HTTP: Запрос 2] ---\n"
            "Корреляция: operUID=a2, rquid=b2"
        )
        assert _strip_correlation_only_http_sections(snippet) == ""

    def test_no_space_after_dashes_still_matched(self) -> None:
        """Заголовок без пробела после --- (e.g. ---[HTTP: ...]) тоже распознаётся."""
        snippet = (
            "---[HTTP: Отправлен запрос] ---\n"
            "Корреляция: operUID=a1, rquid=b1"
        )
        assert _strip_correlation_only_http_sections(snippet) == ""


def test_cluster_uses_representative_correlation_hint() -> None:
    failures = [
        _failure(
            70,
            status_message="Gateway timeout while saving order",
            correlation_hint="operUID=op-1, rqUID=req-1",
        ),
        _failure(
            71,
            status_message="Gateway timeout while saving order",
            correlation_hint="operUID=op-2, rqUID=req-2",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].example_correlation == "operUID=op-1, rqUID=req-1"
    assert report.clusters[0].example_correlation_test_id == 70


def test_cluster_falls_back_to_member_correlation_when_representative_has_none() -> None:
    failures = [
        _failure(
            72,
            status_message="Gateway timeout while saving order",
        ),
        _failure(
            73,
            status_message="Gateway timeout while saving order",
            correlation_hint="operUID=op-73, rqUID=req-73",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].example_correlation == "operUID=op-73, rqUID=req-73"
    assert report.clusters[0].example_correlation_test_id == 73


def test_cluster_reads_correlation_from_old_http_log_sections() -> None:
    failures = [
        _failure(
            74,
            status_message="Gateway timeout while saving order",
            log_snippet=(
                "--- [HTTP: TrRq] ---\n"
                "Корреляция: rqUID=req-74, OperUID=op-74"
            ),
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert report.clusters[0].example_correlation == "operUID=op-74, rqUID=req-74"
    assert report.clusters[0].example_correlation_test_id == 74


# ---------------------------------------------------------------------------
# Тесты уровня кластеров: assertion gate и HTTP-фильтр
# ---------------------------------------------------------------------------


def test_different_assertion_actuals_are_not_merged() -> None:
    """Разные actual-значения в assertion → разные кластеры."""
    failures = [
        _failure(
            60,
            status_message='Operuid 12385734057348907\nНеверный "Status Code" ответа ==> expected: <0> but was: <33>',
        ),
        _failure(
            61,
            status_message='Operuid 12385734057348666\nНеверный "Status Code" ответа ==> expected: <0> but was: <1>',
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2
    member_sets = sorted(tuple(c.member_test_ids) for c in report.clusters)
    assert member_sets == [(60,), (61,)]


def test_different_long_assertion_actuals_are_not_merged() -> None:
    """Длинные assertion-числа в скобках теперь различают кластеры."""
    failures = [
        _failure(62, status_message="expected: <0> but was: <12345>"),
        _failure(63, status_message="expected: <0> but was: <99999>"),
    ]
    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)
    assert report.cluster_count == 2


def test_same_actual_different_expected_still_merged() -> None:
    """Одинаковый actual, разный expected → один кластер."""
    failures = [
        _failure(
            70,
            status_message='Неверный "Status Code" ответа ==> expected: <200> but was: <33>',
        ),
        _failure(
            71,
            status_message='Неверный "Status Code" ответа ==> expected: <0> but was: <33>',
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [70, 71]


def test_different_details_actuals_are_not_merged() -> None:
    """Разные Actual из Details в status_message → разные кластеры."""
    failures = [
        _failure(
            75,
            status_message="AssertionError\n\nExpected: <0>\nActual: <33>",
        ),
        _failure(
            76,
            status_message="AssertionError\n\nExpected: <0>\nActual: <1>",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2
    member_sets = sorted(tuple(c.member_test_ids) for c in report.clusters)
    assert member_sets == [(75,), (76,)]


def test_same_details_actuals_are_merged() -> None:
    """Одинаковый Actual из Details не мешает обычной склейке."""
    failures = [
        _failure(
            77,
            status_message="AssertionError\n\nExpected: <200>\nActual: server-error",
        ),
        _failure(
            78,
            status_message="AssertionError\n\nExpected: <0>\nActual: server-error",
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [77, 78]


def test_correlation_only_http_logs_do_not_merge_different_messages() -> None:
    """Одинаковые correlation-only HTTP логи + разные сообщения → разные кластеры."""
    corr_log = (
        "--- [HTTP: Отправлен запрос -> ] ---\n"
        "Корреляция: operUID=qwe123, rquid=rty456\n\n"
        "--- [HTTP: Отправлен запрос -> ] ---\n"
        "Корреляция: operUID=asd345, rquid=zxc567"
    )
    failures = [
        _failure(
            80,
            status_message="AssertionError: expected true but got false",
            log_snippet=corr_log,
        ),
        _failure(
            81,
            status_message="TimeoutException: request timed out after 30s",
            log_snippet=corr_log,
        ),
    ]

    service = ClusteringService(
        ClusteringConfig(similarity_threshold=0.60, log_similarity_weight=0.15)
    )
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2
    member_sets = sorted(tuple(c.member_test_ids) for c in report.clusters)
    assert member_sets == [(80,), (81,)]


def test_http_section_with_error_still_influences_clustering() -> None:
    """HTTP-секция с ошибкой (не только корреляция) сохраняется и участвует в кластеризации.

    Два теста с разными message, но одинаковой HTTP-ошибкой в логе —
    лог override должен склеить их, т.к. секция содержит реальный error signal.
    """
    http_error_log = (
        "--- [HTTP: Ответ сервера] ---\n"
        "Корреляция: operUID=abc, rquid=def\n"
        "HTTP статус: 502\n"
        "errorMessage: Bad Gateway upstream timeout"
    )
    failures = [
        _failure(
            90,
            status_message="Check response status: expected 200",
            log_snippet=http_error_log,
        ),
        _failure(
            91,
            status_message="Verify gateway response: expected 200",
            log_snippet=http_error_log,
        ),
    ]

    service = ClusteringService(
        ClusteringConfig(similarity_threshold=0.60, log_similarity_weight=0.15)
    )
    report = service.cluster_failures(launch_id=1, failures=failures)

    # HTTP-секция с error signal не вырезается → лог-канал работает →
    # log override склеивает тесты с разными message.
    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [90, 91]


# ---------------------------------------------------------------------------
# Step-path hard gate
# ---------------------------------------------------------------------------


def test_step_path_gate_blocks_log_override() -> None:
    """Регрессия: hard gate по step path применяется ДО log override.

    Один и тот же лог + похожие message обычно склеиваются через log override,
    но разные failed_step_path должны принудительно разделить кластера.
    """
    shared_log = (
        "--- [HTTP: Ответ сервера] ---\n"
        "Корреляция: operUID=abc, rquid=def\n"
        "HTTP статус: 500\n"
        "errorMessage: Internal Server Error"
    )
    failures = [
        _failure(
            100,
            status_message="Check field validation: condition mismatch",
            log_snippet=shared_log,
            failed_step_path="Открыть форму → Проверить поле email",
        ),
        _failure(
            101,
            status_message="Verify form submission: condition failed",
            log_snippet=shared_log,
            failed_step_path="Открыть форму → Проверить поле password",
        ),
    ]

    service = ClusteringService(
        ClusteringConfig(similarity_threshold=0.60, log_similarity_weight=0.15)
    )
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2
    member_sets = sorted(tuple(c.member_test_ids) for c in report.clusters)
    assert member_sets == [(100,), (101,)]


def test_step_path_gate_screen_pol_screen_contract_split() -> None:
    """Целевой кейс: одинаковая ошибка, разные шаги (screen-pol vs screen-contract) → разные кластера."""
    failures = [
        _failure(
            110,
            status_message="AssertionError: условие не выполняется",
            failed_step_path=(
                "Открыть форму → "
                "Для текста в поле qwerty выполняется условие равно screen-pol"
            ),
        ),
        _failure(
            111,
            status_message="AssertionError: условие не выполняется",
            failed_step_path=(
                "Открыть форму → "
                "Для текста в поле qwerty выполняется условие равно screen-contract"
            ),
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2


def test_step_path_gate_same_step_keeps_cluster() -> None:
    """Позитив: одинаковые step paths + похожие message → один кластер (gate не ломает happy path)."""
    same_step = "Открыть форму → Заполнить поле email"
    failures = [
        _failure(
            120,
            status_message="AssertionError: validation failed for input",
            failed_step_path=same_step,
        ),
        _failure(
            121,
            status_message="AssertionError: validation failed for input",
            failed_step_path=same_step,
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [120, 121]


def test_step_path_gate_skipped_when_one_side_missing_path() -> None:
    """Если у одного из failures нет step path, gate не применяется."""
    failures = [
        _failure(
            130,
            status_message="AssertionError: condition mismatch",
            failed_step_path="Открыть форму → Проверить поле foo",
        ),
        _failure(
            131,
            status_message="AssertionError: condition mismatch",
            failed_step_path=None,
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    # Идентичные message + отсутствие step path у одного → старое поведение
    # (gate не режет; пара мерджится).
    assert report.cluster_count == 1


def test_step_path_gate_does_not_merge_transitively_via_hub() -> None:
    """Complete linkage не позволяет A и B (разные шаги) склеиться через C без step path."""
    same_message = "AssertionError: condition mismatch in field"
    failures = [
        _failure(
            140,
            status_message=same_message,
            failed_step_path="Открыть форму → Проверить поле alpha",
        ),
        _failure(
            141,
            status_message=same_message,
            failed_step_path="Открыть форму → Проверить поле beta",
        ),
        _failure(
            142,
            status_message=same_message,
            failed_step_path=None,
        ),
    ]

    service = ClusteringService(ClusteringConfig(similarity_threshold=0.60))
    report = service.cluster_failures(launch_id=1, failures=failures)

    # 140 и 141 не должны оказаться в одном кластере, даже несмотря на хаб 142.
    cluster_by_test = {
        test_id: cluster.cluster_id
        for cluster in report.clusters
        for test_id in cluster.member_test_ids
    }
    assert cluster_by_test[140] != cluster_by_test[141]


def test_step_path_gate_disabled_with_zero_threshold() -> None:
    """ALLURE_CLUSTERING_STEP_STRICT_THRESHOLD=0.0 фактически отключает gate.

    sim < 0.0 невозможно, поэтому пары больше не режутся — те же два failure
    из целевого кейса теперь сливаются в один кластер.
    """
    failures = [
        _failure(
            150,
            status_message="AssertionError: условие не выполняется",
            failed_step_path=(
                "Открыть форму → "
                "Для текста в поле qwerty выполняется условие равно screen-pol"
            ),
        ),
        _failure(
            151,
            status_message="AssertionError: условие не выполняется",
            failed_step_path=(
                "Открыть форму → "
                "Для текста в поле qwerty выполняется условие равно screen-contract"
            ),
        ),
    ]

    service = ClusteringService(
        ClusteringConfig(similarity_threshold=0.60, step_path_strict_threshold=0.0)
    )
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 1
    assert sorted(report.clusters[0].member_test_ids) == [150, 151]


def test_step_path_gate_max_threshold_splits_any_difference() -> None:
    """threshold=1.0 режет пару при любом отличии токенов step path.

    Используем многобуквенные различающиеся слова: TF-IDF дефолтно отсекает
    однобуквенные токены, и step_sim между «Поле A» и «Поле B» оказывается 1.0.
    """
    failures = [
        _failure(
            170,
            status_message="AssertionError: same root cause",
            failed_step_path="Открыть форму → Поле alpha",
        ),
        _failure(
            171,
            status_message="AssertionError: same root cause",
            failed_step_path="Открыть форму → Поле beta",
        ),
    ]

    service = ClusteringService(
        ClusteringConfig(similarity_threshold=0.60, step_path_strict_threshold=1.0)
    )
    report = service.cluster_failures(launch_id=1, failures=failures)

    assert report.cluster_count == 2


# ---------------------------------------------------------------------------
# Gate по ошибкам лога
# ---------------------------------------------------------------------------

ASSERT_500 = "expected: <200> but was: <500>"
POOL_LOG = (
    "2026-10-03 10:00:30 [ERROR] [{thread}] OrderRepository: could not save order {order}\n"
    "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, "
    "request timed out after 30000ms.\n"
    "\tat ru.company.orders.OrderRepository.save(OrderRepository.java:{line})\n"
)
NPE_LOG = (
    "2026-10-03 10:00:00 [ERROR] [{thread}] DiscountService: failed to apply discount {order}\n"
    "java.lang.NullPointerException: Cannot invoke \"Discount.percent()\" because "
    "\"discount\" is null\n"
    "\tat ru.company.orders.DiscountService.apply(DiscountService.java:{line})\n"
)
UNRELATED_ERRORS_LOG = (
    "2026-10-03 10:00:05 [ERROR] [scheduler-1] HealthIndicator: Redis health check failed\n"
    "io.lettuce.core.RedisConnectionException: Unable to connect to redis-cache:6379\n"
    "\tat io.lettuce.core.RedisClient.connect(RedisClient.java:216)\n"
    "2026-10-03 10:00:06 [ERROR] [scheduler-1] KafkaListener: consumer group rebalance failed "
    "for topic orders.events\n"
    "2026-10-03 10:00:07 [ERROR] [scheduler-1] MetricsExporter: push to prometheus-gateway "
    "rejected: payload too large\n"
    "2026-10-03 10:00:08 [ERROR] [scheduler-1] AuditSink: audit queue overflow, dropping events\n"
)


def _log(template: str, thread: str = "http-nio-8080-exec-1", order: int = 5512,
         line: int = 40) -> str:
    return template.format(thread=thread, order=order, line=line)


def _orders(*logs: str | None, first_id: int = 1) -> list[FailedTestSummary]:
    return [
        make_failed_test_summary(
            test_result_id=first_id + index, status_message=ASSERT_500,
            failed_step_path="Отправить запрос POST /orders",
            log_snippet=make_error_log(log) if log else None,
        )
        for index, log in enumerate(logs)
    ]


def _groups(failures: list[FailedTestSummary], config: ClusteringConfig | None = None,
            ) -> list[list[int]]:
    report = ClusteringService(config or ClusteringConfig(log_similarity_weight=0.15)
                               ).cluster_failures(launch_id=1, failures=failures)
    return sorted(sorted(cluster.member_test_ids) for cluster in report.clusters)


def test_log_gate_splits_same_message_with_different_log_errors() -> None:
    """Одинаковые assertion и шаг, в логах пул БД и NPE — разные проблемы."""
    failures = _orders(_log(POOL_LOG), _log(POOL_LOG, order=77), _log(NPE_LOG), _log(NPE_LOG))
    assert _groups(failures) == [[1, 2], [3, 4]]


def test_log_gate_splits_errors_without_exception_classes() -> None:
    """У ошибки nginx класса нет: решает непохожесть документов ошибок."""
    nginx = ('2026/10/03 10:00:00 [error] 77#0: *9 upstream timed out (110: Connection timed '
             'out) while reading response header from upstream, client: 10.0.0.5\n')
    query = ("2026-10-03 10:00:29 [ERROR] ReportRepository: query failed\n"
             "java.sql.SQLTimeoutException: maximum statement execution time exceeded\n"
             "\tat ru.company.reports.ReportRepository.sales(ReportRepository.java:88)\n")
    assert _groups(_orders(nginx, nginx, query)) == [[1, 2], [3]]


def test_log_gate_keeps_one_error_logged_with_noise() -> None:
    """Потоки, номера строк кадров, номера заказов — не другая ошибка."""
    failures = _orders(
        _log(NPE_LOG),
        _log(NPE_LOG, thread="http-nio-8080-exec-7", order=90817, line=41),
        _log(NPE_LOG, thread="pool-3-thread-2", order=12, line=57),
    )
    assert _groups(failures) == [[1, 2, 3]]


def test_log_gate_shared_root_class_outweighs_unrelated_errors() -> None:
    """Посторонние ошибки у одного теста снижают похожесть, но общий корневой класс (NPE)
    говорит, что ошибка та же. Строгий порог показывает: решает ключ, а не похожесть."""
    strict = ClusteringConfig(log_similarity_weight=0.15, log_split_threshold=0.9)
    failures = _orders(_log(NPE_LOG), _log(NPE_LOG, order=77, line=57) + UNRELATED_ERRORS_LOG)
    assert _groups(failures, strict) == [[1, 2]]
    # Без общего ключа такой порог разделяет пару.
    assert _groups(_orders(_log(POOL_LOG), _log(NPE_LOG)), strict) == [[1], [2]]


REDIS_BACKGROUND_LOG = (
    "2026-10-03 10:00:05 [ERROR] [scheduler-1] HealthIndicator: Redis health check failed\n"
    "io.lettuce.core.RedisConnectionException: Unable to connect to redis-cache:6379\n"
    "\tat io.lettuce.core.RedisClient.connect(RedisClient.java:216)\n")
KAFKA_BACKGROUND_LOG = (
    "2026-10-03 10:00:06 [ERROR] [kafka-listener-1] OrderListener: consumer poll failed "
    "for topic orders.events\n"
    "org.apache.kafka.common.errors.RebalanceInProgressException: rebalance in progress\n"
    "\tat org.apache.kafka.clients.consumer.KafkaConsumer.poll(KafkaConsumer.java:1250)\n")


def _other_symptom(test_id: int, log: str) -> FailedTestSummary:
    """Та же ошибка сервиса за другим симптомом: клиент бросил исключение вместо 500."""
    return make_failed_test_summary(
        test_result_id=test_id,
        status_message='HttpServerErrorException: 500 Internal Server Error: "merge failed"',
        failed_step_path="Отправить запрос POST /orders", log_snippet=make_error_log(log))


@pytest.mark.parametrize("third", [None, "same error, other symptom"])
def test_log_gate_keeps_one_error_with_different_background(third: str | None) -> None:
    """Одна NPE в обоих логах, вокруг разный фон: общая ошибка держит пару — и когда та же
    NPE есть у падения с другим симптомом (причина с несколькими симптомами — не фон)."""
    failures = _orders(_log(NPE_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + KAFKA_BACKGROUND_LOG)
    if third:
        failures.append(_other_symptom(9, _log(NPE_LOG)))
    assert [1, 2] in _groups(failures)


def test_log_gate_shared_background_keeps_the_pair() -> None:
    """Одинаковая фоновая ошибка в обоих логах неотличима от общей ошибки падения: пару не
    делим (осторожная сторона, различает модель по примерам)."""
    failures = _orders(_log(POOL_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + REDIS_BACKGROUND_LOG)
    failures.append(_other_symptom(9, REDIS_BACKGROUND_LOG))
    assert [1, 2] in _groups(failures)


def test_log_gate_clean_log_separates_groups_despite_shared_background() -> None:
    """Достаточно одной пары без общей ошибки: complete linkage не сводит пул БД с NPE,
    у которой фона нет."""
    failures = _orders(_log(POOL_LOG) + REDIS_BACKGROUND_LOG,
                       _log(NPE_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG, order=77))
    assert _groups(failures) == [[1], [2, 3]]


def test_log_gate_keeps_a_log_whose_errors_are_all_in_the_other() -> None:
    """Свои ошибки только у одного лога — у второго разделять нечем, даже при строгом пороге."""
    strict = ClusteringConfig(log_similarity_weight=0.15, log_split_threshold=0.9)
    failures = _orders(_log(NPE_LOG), _log(NPE_LOG) + UNRELATED_ERRORS_LOG)
    assert _groups(failures, strict) == [[1, 2]]


def test_log_gate_ignores_wrapper_exceptions() -> None:
    """Общая обёртка — не общая ошибка: ключ — корневой класс (последний ``Caused by``)."""
    def wrapped(cause: str) -> str:
        return ("2026-10-03 10:00:00 [ERROR] DispatcherServlet: Request processing failed\n"
                "jakarta.servlet.ServletException: Request processing failed\n"
                "\tat org.springframework.web.servlet.FrameworkServlet.processRequest"
                "(FrameworkServlet.java:1022)\n"
                f"Caused by: {cause}\n"
                "\t... 42 more\n")
    pool = wrapped("java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is "
                   "not available, request timed out after 30000ms.")
    npe = wrapped("java.lang.NullPointerException: Cannot invoke \"Discount.percent()\" "
                  "because \"discount\" is null")
    keys = [[block.keys for block in _log_error_blocks(f, head_lines=50, tail_lines=50)]
            for f in _orders(pool, npe)]
    assert keys == [[frozenset({"SQLTransientConnectionException"})],
                    [frozenset({"NullPointerException"})]]


def test_log_gate_needs_errors_in_both_logs() -> None:
    """Нет лога, только HTTP-секция или журнал — gate не применяется."""
    http = ("--- [HTTP: response.json] ---\nHTTP статус: 500\n"
            "errorMessage: discount is null")
    failures = _orders(_log(POOL_LOG), None)
    failures.append(make_failed_test_summary(
        test_result_id=3, status_message=ASSERT_500,
        failed_step_path="Отправить запрос POST /orders", log_snippet=http))
    assert _groups(failures) == [[1, 2, 3]]


def test_log_gate_does_not_merge_two_errors_through_a_test_without_log() -> None:
    """Тест без лога похож на оба, но complete linkage не сводит пул БД и NPE."""
    groups = _groups(_orders(_log(POOL_LOG), None, _log(NPE_LOG)))
    assert not any({1, 3} <= set(group) for group in groups)


@pytest.mark.parametrize("config", [
    ClusteringConfig(log_similarity_weight=0.15, log_split_threshold=0.0),
    ClusteringConfig(log_similarity_weight=0.0),
])
def test_log_gate_is_off_with_zero_threshold_or_zero_log_weight(
    config: ClusteringConfig,
) -> None:
    failures = _orders(_log(POOL_LOG), _log(NPE_LOG))
    assert _groups(failures, config) == [[1, 2]]


# ---------------------------------------------------------------------------
# Gate по ресурсам сообщения (хосты, локаторы)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("message", "hosts", "locators"), [
    ("java.net.ConnectException: Connection refused: auth-service:8080",
     {"auth-service:8080"}, set()),
    ("java.net.ConnectException: Failed to connect to inventory.svc/10.2.0.7:8080",
     {"inventory.svc:8080"}, set()),
    ("java.net.ConnectException: Failed to connect to localhost/127.0.0.1:5432",
     {"localhost:5432"}, set()),
    ("Connection refused: Auth-Service:8080", {"auth-service:8080"}, set()),
    ("Connect to billing:8443 [billing/10.0.0.3] failed: Connection refused",
     {"billing:8443"}, set()),
    ("connect ECONNREFUSED 10.1.2.3:5432", {"<ip>:5432"}, set()),
    ('I/O error on GET request for "http://pricing-2.internal/api/prices": Read timed out',
     {"pricing-#.internal"}, set()),
    ("java.net.UnknownHostException: geo.internal: Name or service not known", set(), set()),
    ("getaddrinfo ENOTFOUND api.stage.local", set(), set()),
    ("Element not found {#checkout-button}\nExpected: visible", set(), {"#checkout-button"}),
    ("Element not found {#Login}", set(), {"#Login"}),
    ("Element should be visible {.cart li:nth-child(2)}", set(), {".cart li:nth-child(#)"}),
    ('no such element: Unable to locate element: {"method":"css selector","selector":"#promo"}',
     set(), {'{"method":"css selector","selector":"#promo"}'}),
    ("waiting for visibility of element located by By.cssSelector: #login-form",
     set(), {"#login-form"}),
    ("TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
     "  - waiting for getByRole('button', { name: 'Apply' })",
     set(), {"getByRole('button', { name: 'Apply' })"}),
    ("Timed out retrying: Expected to find element: `[data-cy=submit]`, but never found it.",
     set(), {"[data-cy=submit]"}),
    ('Waiting for selector "#app .ready" failed: timeout 30000ms exceeded', set(),
     {"#app .ready"}),
    ("Waiting for selector `[data-test=\"submit\"]` failed", set(), {'[data-test="submit"]'}),
    ("waiting for selector '[data-test=\"cancel\"]' failed", set(), {'[data-test="cancel"]'}),
    # Не ресурсы: имена классов и файлов, тестовые данные в кавычках.
    ("java.lang.IllegalStateException at ru.company.orders.OrderService (OrderService.java:40)",
     set(), set()),
    ("Login failed: user 'ivanov' is locked after 3 failed attempts", set(), set()),
])
def test_message_resources(message: str, hosts: set[str], locators: set[str]) -> None:
    resources = message_resources(message)
    assert (resources.hosts, resources.locators) == (hosts, locators)


def _calls(*messages: str, step: str = "Вызвать сервис", logs: tuple[str | None, ...] = (),
           ) -> list[FailedTestSummary]:
    trace = ("java.net.ConnectException: connect failed\n"
             "\tat ru.company.platform.HttpGateway.call(HttpGateway.java:64)\n")
    padded = (*logs, *(None,) * (len(messages) - len(logs)))
    return [
        make_failed_test_summary(
            test_result_id=index + 1, status_message=message, status_trace=trace,
            failed_step_path=step, log_snippet=make_error_log(log) if log else None,
        )
        for index, (message, log) in enumerate(zip(messages, padded))
    ]


def _connect(host: str) -> str:
    return f"java.net.ConnectException: Failed to connect to {host}/10.2.0.7:8080"


def test_resource_gate_splits_different_hosts_of_one_message() -> None:
    failures = _calls(_connect("inventory.svc"), _connect("inventory.svc"),
                      _connect("loyalty.svc"))
    assert _groups(failures) == [[1, 2], [3]]


@pytest.mark.parametrize("pair", [
    # Порт после «/IP»: в TF-IDF сообщения четырёхзначные порты сливаются в <NUM>.
    ("java.net.ConnectException: Failed to connect to localhost/127.0.0.1:8080",
     "java.net.ConnectException: Failed to connect to localhost/127.0.0.1:5432"),
    # DOM id чувствителен к регистру, а TF-IDF — нет.
    ("Element not found {#Login}\nExpected: visible",
     "Element not found {#login}\nExpected: visible"),
    # Кавычки внутри локатора Puppeteer.
    ("Waiting for selector '[data-test=\"submit\"]' failed: timeout 30000ms exceeded",
     "Waiting for selector '[data-test=\"cancel\"]' failed: timeout 30000ms exceeded"),
])
def test_resource_gate_splits_resources_that_text_similarity_misses(
    pair: tuple[str, str],
) -> None:
    assert _groups(_calls(*pair)) == [[1], [2]]


def test_resource_gate_splits_different_locators_at_the_message_threshold() -> None:
    """Сообщения Playwright различаются только локатором: сходство ровно на пороге."""
    def timeout(locator: str) -> str:
        return (f"TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
                f"waiting for locator('{locator}')")
    failures = _calls(timeout("[data-test=price-filter]"), timeout("iframe#payment-frame"))
    assert _groups(failures) == [[1], [2]]


@pytest.mark.parametrize("messages", [
    # Реплики одного сервиса.
    (_connect("orders-1.svc"), _connect("orders-2.svc")),
    # Значения в кавычках — тестовые данные, а не ресурс.
    ("Login failed: user 'ivanov' is locked after 3 failed attempts",
     "Login failed: user 'petrova' is locked after 3 failed attempts"),
    # Сбой DNS бьёт по всем хостам: ошибка разрешения имени хостов не даёт.
    ("java.net.UnknownHostException: cdn.stage.local: Temporary failure in name resolution",
     "java.net.UnknownHostException: auth.stage.local: Temporary failure in name resolution"),
])
def test_resource_gate_keeps_one_problem(messages: tuple[str, str]) -> None:
    assert _groups(_calls(*messages)) == [[1, 2]]


def test_same_log_still_merges_different_locators() -> None:
    """Страница не загрузилась: элементы разные, ошибка скрипта в логе одна — log override."""
    console = ("2026-10-03 10:03:01 [SEVERE] http://shop.test/static/catalog.js 1:20451 "
               "Uncaught TypeError: Cannot read properties of undefined (reading 'items')\n")
    failures = _calls("Element not found {.product-card}\nExpected: visible",
                      "Element not found {.filters-panel}\nExpected: visible",
                      logs=(console, console))
    assert _groups(failures) == [[1, 2]]
    assert _groups(_calls(*(f.status_message or "" for f in failures))) == [[1], [2]]


def test_resource_gate_can_be_turned_off() -> None:
    failures = _calls(_connect("inventory.svc"), _connect("loyalty.svc"))
    assert _groups(failures, ClusteringConfig(resource_gate=False)) == [[1, 2]]


def test_cluster_without_error_text_is_labelled_by_test_name() -> None:
    # Метка попадает в сводку и отчёт: «Тест: 301» читателю ничего не говорит (стенд Qwen, E03).
    report = ClusteringService(ClusteringConfig()).cluster_failures(
        launch_id=1, failures=[_failure(301)])
    assert [cluster.label for cluster in report.clusters] == ["Тест: test-301"]


# ---------------------------------------------------------------------------
# Счётчики gates (ClusteringGateStats)
# ---------------------------------------------------------------------------


def _gates(failures: list[FailedTestSummary], config: ClusteringConfig | None = None,
           ) -> dict[str, int]:
    report = ClusteringService(config or ClusteringConfig(log_similarity_weight=0.15)
                               ).cluster_failures(launch_id=1, failures=failures)
    return report.gates.model_dump()


def _nonzero(gates: dict[str, int]) -> dict[str, int]:
    return {key: value for key, value in gates.items() if value}


def test_gates_count_assertion_split() -> None:
    failures = [_failure(1, status_message="Status ==> expected: <0> but was: <33>"),
                _failure(2, status_message="Status ==> expected: <0> but was: <1>")]
    assert _nonzero(_gates(failures)) == {"pairs": 1, "assertion_split": 1}


def test_gates_count_step_split_before_log_override() -> None:
    """Пару разрезал шаг — log override и gate по логу её уже не видят."""
    failures = _orders(_log(NPE_LOG), _log(NPE_LOG))
    failures[1] = failures[1].model_copy(update={"failed_step_path": "Отменить заказ"})
    assert _nonzero(_gates(failures)) == {"pairs": 1, "step_split": 1}


def test_gates_count_log_split_and_problem_pairs() -> None:
    failures = _orders(_log(POOL_LOG), _log(POOL_LOG, order=77), _log(NPE_LOG), _log(NPE_LOG))
    assert _nonzero(_gates(failures)) == {
        "pairs": 6, "pairs_in_one_problem": 2, "log_pairs": 6, "log_split": 4}


def test_gates_count_pair_held_by_shared_root_class() -> None:
    """Два разных NPE за одним ассертом: общих блоков нет, логи непохожи, держит ключ."""
    other_npe = (
        "2026-10-03 10:00:00 [ERROR] [{thread}] PriceCalculator: cart total unavailable\n"
        "java.lang.NullPointerException: Cannot read field \"amount\" because \"tax\" is null\n"
        "\tat ru.company.orders.PriceCalculator.total(PriceCalculator.java:{line})\n")
    gates = _gates(_orders(_log(NPE_LOG), _log(other_npe)))
    assert _nonzero(gates) == {"pairs": 1, "pairs_in_one_problem": 1, "log_pairs": 1,
                               "log_held_by_key": 1, "log_held_merged": 1}


def test_gates_count_pair_held_by_shared_background() -> None:
    """Общий Redis поднимает похожесть пула и NPE выше порога (0.52): по похожести пару не
    отличить от одной ошибки — без общего блока gate разделил бы её."""
    failures = _orders(_log(POOL_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + REDIS_BACKGROUND_LOG)
    assert _nonzero(_gates(failures)) == {"pairs": 1, "pairs_in_one_problem": 1, "log_pairs": 1,
                                          "log_held_by_block": 1, "log_held_merged": 1}


def test_gates_count_one_error_with_different_background_as_held_by_block() -> None:
    """Обратный случай неотличим по паре: общая NPE, разный фон — тоже «держит общий блок»."""
    failures = _orders(_log(NPE_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + KAFKA_BACKGROUND_LOG)
    assert _gates(failures)["log_held_by_block"] == 1


@pytest.mark.parametrize("logs", [
    (NPE_LOG, NPE_LOG),  # одинаковые блоки — разделять нечем
    (NPE_LOG, NPE_LOG + UNRELATED_ERRORS_LOG),  # ошибки одного лога целиком в другом
])
def test_gates_do_not_count_pairs_without_own_errors_on_both_sides(logs: tuple[str, str]) -> None:
    gates = _gates(_orders(*(_log(log) for log in logs)))
    assert (gates["log_pairs"], gates["log_held_by_block"], gates["log_held_by_key"]) == (1, 0, 0)


def test_gates_count_message_split() -> None:
    failures = _calls("java.net.ConnectException: Connection refused",
                      "java.lang.IllegalStateException: order is already closed")
    assert _nonzero(_gates(failures)) == {"pairs": 1, "message_split": 1}


def test_gates_count_resource_split() -> None:
    failures = _calls(_connect("inventory.svc"), _connect("loyalty.svc"))
    assert _nonzero(_gates(failures)) == {"pairs": 1, "resource_split": 1}


def test_gates_count_log_override() -> None:
    console = ("2026-10-03 10:03:01 [SEVERE] http://shop.test/static/catalog.js 1:20451 "
               "Uncaught TypeError: Cannot read properties of undefined (reading 'items')\n")
    failures = _calls("Element not found {.product-card}\nExpected: visible",
                      "Element not found {.filters-panel}\nExpected: visible",
                      logs=(console, console))
    assert _nonzero(_gates(failures)) == {
        "pairs": 1, "pairs_in_one_problem": 1, "log_pairs": 1, "log_override": 1,
        "log_override_resources": 1, "log_override_merged": 1}


@pytest.mark.parametrize("config", [
    ClusteringConfig(log_similarity_weight=0.15, log_split_threshold=0.0),
    ClusteringConfig(log_similarity_weight=0.0),
])
def test_gates_log_counters_are_zero_when_log_gate_is_off(config: ClusteringConfig) -> None:
    failures = _orders(_log(POOL_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + REDIS_BACKGROUND_LOG,
                       _log(POOL_LOG), _log(NPE_LOG))
    gates = _gates(failures, config)
    assert gates["pairs"] == 6
    assert not any(gates[key] for key in ("log_pairs", "log_split", "log_held_by_key",
                                          "log_held_by_block", "log_held_merged"))


def test_gates_are_zero_without_pairs() -> None:
    assert not any(_gates([]).values())
    assert _nonzero(_gates(_orders(_log(NPE_LOG)))) == {}


def test_gate_pairs_carry_test_ids_but_stay_out_of_the_dump() -> None:
    failures = _orders(_log(POOL_LOG) + REDIS_BACKGROUND_LOG, _log(NPE_LOG) + REDIS_BACKGROUND_LOG,
                       first_id=40)
    report = ClusteringService(ClusteringConfig(log_similarity_weight=0.15)
                               ).cluster_failures(launch_id=1, failures=failures)
    assert report.gates.held_test_pairs == [(40, 41)]
    assert "held_test_pairs" not in report.model_dump()["gates"]
    assert "override_test_pairs" not in report.model_dump(mode="json")["gates"]


def test_gates_split_counters_never_exceed_pairs() -> None:
    failures = [
        *_orders(_log(POOL_LOG), _log(NPE_LOG) + REDIS_BACKGROUND_LOG, None),
        *_calls(_connect("inventory.svc"), _connect("loyalty.svc"), "Status: but was: <1>"),
    ]
    failures = [f.model_copy(update={"test_result_id": index}) for index, f in enumerate(failures)]
    gates = _gates(failures)
    decided = (gates["assertion_split"] + gates["step_split"] + gates["log_split"]
               + gates["log_held_by_key"] + gates["log_held_by_block"] + gates["message_split"]
               + gates["resource_split"] + gates["log_override"])
    assert 0 < decided <= gates["pairs"] == 15
    assert gates["log_pairs"] <= gates["pairs"]


def test_gates_held_by_block_is_the_same_in_small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остатки повторяются (три пула и три NPE за одним фоном — одна пара остатков на девять
    пар тестов) и считаются пакетами: результат от размера пакета не зависит."""
    other_pool = POOL_LOG.replace("OrderRepository: could not save", "CartRepository: could not load")
    failures = _orders(*(_log(POOL_LOG, order=index) + REDIS_BACKGROUND_LOG for index in range(3)),
                       *(_log(NPE_LOG, order=index) + REDIS_BACKGROUND_LOG for index in range(3)),
                       _log(other_pool) + KAFKA_BACKGROUND_LOG, _log(NPE_LOG) + KAFKA_BACKGROUND_LOG)
    default = _gates(failures)
    monkeypatch.setattr(_LogErrorGate, "PAIR_BATCH", 1)
    assert _gates(failures) == default
    assert default["log_held_by_block"] >= 9
