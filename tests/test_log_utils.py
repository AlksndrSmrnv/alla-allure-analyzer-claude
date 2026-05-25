"""Тесты для утилит разбора лог-секций."""

import pytest

from alla.utils.log_utils import (
    extract_correlation_from_log,
    extract_correlation_pairs_from_json,
    extract_correlation_pairs_from_text,
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


def test_parse_correlation_line_drops_type_name_placeholders() -> None:
    line = "Корреляция: operUID=String, rqUID=String"

    assert parse_correlation_line(line) == {}


def test_parse_correlation_line_picks_real_value_after_placeholder() -> None:
    line = "Корреляция: operUID=String, operUID=ab12cd34"

    assert parse_correlation_line(line) == {"operUID": "ab12cd34"}


@pytest.mark.parametrize("placeholder", ["string", "STRING", "String", " String "])
def test_parse_correlation_line_drops_placeholders_case_insensitive(
    placeholder: str,
) -> None:
    line = f"Корреляция: rqUID={placeholder}, rqUID=real-id-123"

    assert parse_correlation_line(line) == {"rqUID": "real-id-123"}


def test_parse_correlation_line_keeps_short_legacy_ids() -> None:
    """Legacy HTTP-логи часто содержат 3-символьные id (см. test_clustering_service)."""
    line = "Корреляция: operUID=abc, rqUID=def"

    assert parse_correlation_line(line) == {"operUID": "abc", "rqUID": "def"}


def test_parse_correlation_line_keeps_full_value_with_special_chars() -> None:
    """Значение со спец-символами и точками целиком сохраняется, а не обрезается."""
    line = "Корреляция: traceId=svc.v1.req-12345/xyz, rqUID=req-1"

    assert parse_correlation_line(line) == {
        "traceId": "svc.v1.req-12345/xyz",
        "rqUID": "req-1",
    }


def test_parse_correlation_line_does_not_truncate_long_values() -> None:
    """Значения длиннее 64 символов не должны обрезаться regex-ом."""
    long_value = "a" * 96
    line = f"Корреляция: traceId={long_value}"

    assert parse_correlation_line(line) == {"traceId": long_value}


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


def test_extract_correlation_from_log_skips_placeholder_line() -> None:
    log_snippet = (
        "--- [HTTP: response.json] ---\n"
        "Корреляция: operUID=String, rqUID=String\n"
        "Корреляция: rqUID=real-id-12345\n"
        "HTTP статус: 503"
    )

    assert extract_correlation_from_log(log_snippet) == "rqUID=real-id-12345"


def test_extract_correlation_pairs_from_text_json_format() -> None:
    text = '{"RqUID": "abc-123", "message": "failed"}'

    assert extract_correlation_pairs_from_text(text) == {"RqUID": "abc-123"}


def test_extract_correlation_pairs_from_text_drops_placeholder_json() -> None:
    text = '{"rqUID": "String", "OperUID": "12345abc"}'

    assert extract_correlation_pairs_from_text(text) == {"OperUID": "12345abc"}


def test_extract_correlation_pairs_from_text_kv_format() -> None:
    text = "OperUID=op-1\nrequestId: req-1"

    assert extract_correlation_pairs_from_text(text) == {
        "OperUID": "op-1",
        "requestId": "req-1",
    }


def test_extract_correlation_pairs_from_text_drops_placeholder_kv() -> None:
    text = "RqUID: String\nRqUID: 12345abc"

    assert extract_correlation_pairs_from_text(text) == {"RqUID": "12345abc"}


def test_extract_correlation_pairs_from_text_drops_common_type_names() -> None:
    text = "rqUID=Long\noperUID=Integer\ntraceId: real-trace-42"

    assert extract_correlation_pairs_from_text(text) == {"traceId": "real-trace-42"}


def test_extract_correlation_pairs_from_text_xml_format() -> None:
    text = "<traceId>tr-1</traceId>"

    assert extract_correlation_pairs_from_text(text) == {"traceId": "tr-1"}


def test_extract_correlation_pairs_from_text_mixed_in_one_blob() -> None:
    text = (
        'Caused by: HttpError("RqUID":"abc-1")\n'
        "OperUID=op-1 not found\n"
        "<traceId>tr-1</traceId>"
    )

    assert extract_correlation_pairs_from_text(text) == {
        "RqUID": "abc-1",
        "OperUID": "op-1",
        "traceId": "tr-1",
    }


def test_extract_correlation_pairs_from_text_empty_and_no_match() -> None:
    assert extract_correlation_pairs_from_text("") == {}
    assert extract_correlation_pairs_from_text("plain exception text") == {}


def test_extract_correlation_pairs_from_text_case_insensitive() -> None:
    text = "RQUID=req-1\noperuid=op-1\ntraceId: tr-1"

    assert extract_correlation_pairs_from_text(text) == {
        "RQUID": "req-1",
        "operuid": "op-1",
        "traceId": "tr-1",
    }


def test_extract_correlation_pairs_from_text_first_wins_on_duplicate() -> None:
    text = "RqUID=first\nRqUID=second\nrequestId=req-1"

    assert extract_correlation_pairs_from_text(text) == {
        "RqUID": "first",
        "requestId": "req-1",
    }


def test_extract_correlation_pairs_from_json_top_level() -> None:
    obj = {"RqUID": "abc-123", "statusCode": 500}

    assert extract_correlation_pairs_from_json(obj) == {"RqUID": "abc-123"}


def test_extract_correlation_pairs_from_json_drops_top_level_placeholder() -> None:
    obj = {"RqUID": "String", "headers": {"OperUID": "real-id-123"}}

    assert extract_correlation_pairs_from_json(obj) == {"OperUID": "real-id-123"}


def test_extract_correlation_pairs_from_json_nested_dict() -> None:
    obj = {"headers": {"OperUID": "op-1"}}

    assert extract_correlation_pairs_from_json(obj) == {"OperUID": "op-1"}


def test_extract_correlation_pairs_from_json_in_list_of_dicts() -> None:
    obj = [{"message": "first"}, {"traceId": "tr-1"}]

    assert extract_correlation_pairs_from_json(obj) == {"traceId": "tr-1"}


def test_extract_correlation_pairs_from_json_respects_max_depth() -> None:
    obj: dict[str, object] = {}
    current = obj
    for index in range(11):
        child: dict[str, object] = {}
        current[f"level{index}"] = child
        current = child
    current["RqUID"] = "too-deep"

    assert extract_correlation_pairs_from_json(obj, max_depth=10) == {}


def test_extract_correlation_pairs_from_json_ignores_non_scalar_values() -> None:
    obj = {"RqUID": {"value": "nested"}, "OperUID": ["op-1"], "traceId": "tr-1"}

    assert extract_correlation_pairs_from_json(obj) == {"traceId": "tr-1"}
