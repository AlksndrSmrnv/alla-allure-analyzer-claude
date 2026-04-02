"""Тесты для утилит разбора лог-секций."""

from alla.utils.log_utils import (
    extract_correlation_from_log,
    parse_correlation_line,
    parse_log_sections,
)


def test_parse_log_sections_splits_file_and_http_sections() -> None:
    log_snippet = (
        "--- [файл: app.log] ---\n"
        "2026-03-31 [ERROR] Connection refused\n"
        "\n"
        "--- [HTTP: response.json] ---\n"
        "HTTP статус: 503\n"
        "error: Service unavailable"
    )

    assert parse_log_sections(log_snippet) == [
        ("app.log", "2026-03-31 [ERROR] Connection refused"),
        ("HTTP: response.json", "HTTP статус: 503\nerror: Service unavailable"),
    ]


def test_parse_log_sections_can_skip_http_sections() -> None:
    log_snippet = (
        "--- [файл: app.log] ---\n"
        "retry budget exhausted while saving order\n"
        "\n"
        "--- [HTTP: response.json] ---\n"
        "HTTP статус: 503\n"
        "error: Service unavailable"
    )

    assert parse_log_sections(log_snippet, include_http=False) == [
        ("app.log", "retry budget exhausted while saving order")
    ]


def test_parse_log_sections_returns_empty_when_only_http_is_filtered_out() -> None:
    log_snippet = (
        "--- [HTTP: response.json] ---\n"
        "HTTP статус: 503\n"
        "error: Service unavailable"
    )

    assert parse_log_sections(log_snippet, include_http=False) == []


def test_parse_correlation_line_normalizes_keys_and_keeps_first_value() -> None:
    line = "Корреляция: OperUID=op-1, rqUID=req-1, operUID=op-2"

    assert parse_correlation_line(line) == {
        "operUID": "op-1",
        "rqUID": "req-1",
    }


def test_extract_correlation_from_log_reads_first_http_section() -> None:
    log_snippet = (
        "--- [файл: app.log] ---\n"
        "2026-03-31 [ERROR] Connection refused\n"
        "\n"
        "--- [HTTP: request.json] ---\n"
        "Корреляция: rqUID=req-1, OperUID=op-1\n"
        "\n"
        "--- [HTTP: response.json] ---\n"
        "Корреляция: OperUID=op-2, rqUID=req-2\n"
        "HTTP статус: 503\n"
        "error: Service unavailable"
    )

    assert extract_correlation_from_log(log_snippet) == "operUID=op-1, rqUID=req-1"
