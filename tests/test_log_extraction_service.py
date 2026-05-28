"""Тесты для LogExtractionService — извлечение ERROR-блоков из логов."""

from alla.services.log_extraction_service import _extract_error_blocks


class TestExtractErrorBlocks:
    """Тесты для _extract_error_blocks."""

    def test_single_error_with_stacktrace(self):
        """Один [ERROR] с полным stack trace."""
        log = (
            "2026-02-09 10:23:44,100 [INFO] Starting test\n"
            "2026-02-09 10:23:45,123 [ERROR] NullPointerException in UserService\n"
            "    at com.example.UserService.getUser(UserService.java:42)\n"
            "    at com.example.Controller.handle(Controller.java:15)\n"
            "    at sun.reflect.NativeMethodAccessorImpl.invoke(NativeMethodAccessorImpl.java:62)\n"
            "2026-02-09 10:23:46,200 [INFO] Test finished"
        )
        result = _extract_error_blocks(log)
        assert "[ERROR] NullPointerException" in result
        assert "UserService.java:42" in result
        assert "Controller.java:15" in result
        assert "[INFO] Starting test" not in result
        assert "[INFO] Test finished" not in result

    def test_multiple_errors(self):
        """Несколько [ERROR] блоков — все извлекаются."""
        log = (
            "2026-02-09 10:23:44,100 [INFO] Boot\n"
            "2026-02-09 10:23:45,123 [ERROR] First error\n"
            "    at com.example.A.foo(A.java:1)\n"
            "2026-02-09 10:23:46,200 [INFO] In between\n"
            "2026-02-09 10:23:47,300 [ERROR] Second error\n"
            "    at com.example.B.bar(B.java:2)\n"
            "2026-02-09 10:23:48,400 [INFO] Done"
        )
        result = _extract_error_blocks(log)
        assert "First error" in result
        assert "A.java:1" in result
        assert "Second error" in result
        assert "B.java:2" in result
        assert "[INFO]" not in result

    def test_no_errors_returns_empty(self):
        """Лог без [ERROR] → пустая строка."""
        log = (
            "2026-02-09 10:23:44,100 [INFO] Starting test\n"
            "2026-02-09 10:23:45,123 [DEBUG] Connecting to DB\n"
            "2026-02-09 10:23:46,200 [INFO] Test finished"
        )
        result = _extract_error_blocks(log)
        assert result == ""

    def test_error_case_insensitive(self):
        """[Error], [error], [ERROR] — все находятся."""
        log = (
            "2026-02-09 10:23:45,123 [Error] Mixed case error\n"
            "2026-02-09 10:23:46,200 [INFO] gap\n"
            "2026-02-09 10:23:47,300 [error] Lower case error\n"
            "2026-02-09 10:23:48,400 [INFO] end"
        )
        result = _extract_error_blocks(log)
        assert "Mixed case error" in result
        assert "Lower case error" in result

    def test_stacktrace_stops_at_non_error_log_line(self):
        """Stack trace заканчивается при встрече новой лог-записи без [ERROR]."""
        log = (
            "2026-02-09 10:23:45,123 [ERROR] Exception occurred\n"
            "    at com.example.Main.run(Main.java:10)\n"
            "    at com.example.Main.main(Main.java:5)\n"
            "Caused by: java.io.IOException: Connection refused\n"
            "    at com.example.Net.connect(Net.java:33)\n"
            "2026-02-09 10:23:46,200 [WARN] Recovery attempted\n"
            "2026-02-09 10:23:47,300 [INFO] Done"
        )
        result = _extract_error_blocks(log)
        assert "Exception occurred" in result
        assert "Main.java:10" in result
        assert "Connection refused" in result
        assert "Net.java:33" in result
        assert "[WARN]" not in result
        assert "[INFO]" not in result

    def test_consecutive_errors_without_gap(self):
        """Два [ERROR] подряд без промежуточных строк."""
        log = (
            "2026-02-09 10:23:45,123 [ERROR] Error one\n"
            "2026-02-09 10:23:45,124 [ERROR] Error two\n"
            "    stacktrace line\n"
            "2026-02-09 10:23:46,200 [INFO] Done"
        )
        result = _extract_error_blocks(log)
        assert "Error one" in result
        assert "Error two" in result
        assert "stacktrace line" in result

    def test_error_at_end_of_log(self):
        """[ERROR] в конце лога без последующей записи."""
        log = (
            "2026-02-09 10:23:44,100 [INFO] Starting\n"
            "2026-02-09 10:23:45,123 [ERROR] Final error\n"
            "    at com.example.End.crash(End.java:99)"
        )
        result = _extract_error_blocks(log)
        assert "Final error" in result
        assert "End.java:99" in result

    def test_empty_log(self):
        """Пустой лог."""
        assert _extract_error_blocks("") == ""

    def test_error_with_iso_timestamp(self):
        """[ERROR] с ISO timestamp (T-separator)."""
        log = (
            "2026-02-09T10:23:45.123Z [ERROR] ISO format error\n"
            "    at some.Class.method(File.java:1)\n"
            "2026-02-09T10:23:46.200Z [INFO] Done"
        )
        result = _extract_error_blocks(log)
        assert "ISO format error" in result
        assert "File.java:1" in result
        assert "[INFO]" not in result


from unittest.mock import patch

from alla.services.log_extraction_service import _decode_text, _detect_content_type


class TestDetectContentType:
    def test_json_bytes_detected_as_json(self):
        content = b'{"key": "value", "status": 500}'
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="application/json"):
            assert _detect_content_type(content) == "json"

    def test_text_plain_detected_as_text(self):
        content = b"2026-01-01 [ERROR] something failed"
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            assert _detect_content_type(content) == "text"

    def test_xml_bytes_detected_as_xml(self):
        content = b"<?xml version='1.0'?><root><error>fail</error></root>"
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="application/xml"):
            assert _detect_content_type(content) == "xml"

    def test_binary_image_returns_binary(self):
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="image/png"):
            assert _detect_content_type(content) == "binary"

    def test_json_heuristic_when_magic_says_text(self):
        """Если magic вернул text/plain, но контент начинается с {, считать json."""
        content = b'{"RqUID": "abc"}'
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            assert _detect_content_type(content) == "json"

    def test_magic_unavailable_falls_back_to_fallback_mime(self):
        content = b'{"key": "value"}'
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", False):
            assert _detect_content_type(content, fallback_mime="application/json") == "json"

    def test_magic_unavailable_binary_fallback(self):
        content = b"\x89PNG\r\n"
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", False):
            assert _detect_content_type(content, fallback_mime="image/png") == "binary"


class TestDecodeText:
    def test_utf8_text_decoded(self):
        content = "Ошибка подключения".encode("utf-8")
        result = _decode_text(content)
        assert result is not None
        assert "Ошибка" in result

    def test_latin1_text_decoded(self):
        content = "connection error".encode("latin-1")
        result = _decode_text(content)
        assert result is not None
        assert "connection" in result

    def test_binary_returns_none(self):
        content = bytes(range(256)) * 4
        result = _decode_text(content)
        assert result is None


from alla.services.log_extraction_service import _extract_text_http_info


class TestExtractTextHttpInfo:
    def test_extracts_rquid_from_json_text(self):
        text = '{"RqUID": "abc-123", "statusCode": 500, "error": "Service unavailable"}'
        result = _extract_text_http_info(text)
        assert "RqUID=abc-123" in result
        assert "500" in result
        assert "Service unavailable" in result

    def test_extracts_operuid_from_kv_format(self):
        text = "OperUID=xyz-456\nHTTP/1.1 503 Service Unavailable"
        result = _extract_text_http_info(text)
        assert "OperUID=xyz-456" in result
        assert "503" in result

    def test_http_status_line_extracted(self):
        text = "HTTP/1.1 404 Not Found\n{\"message\": \"not found\"}"
        result = _extract_text_http_info(text)
        assert "404" in result

    def test_xml_corr_id_extracted(self):
        text = "<RqUID>req-789</RqUID><fault><faultCode>ERR</faultCode></fault>"
        result = _extract_text_http_info(text)
        assert "req-789" in result
        assert "ERR" in result

    def test_message_included_only_with_error_signal(self):
        """message без признака ошибки не включается."""
        text = '{"message": "All systems operational"}'
        result = _extract_text_http_info(text)
        assert result == ""

    def test_message_included_when_error_present(self):
        """message включается если рядом есть error-поле."""
        text = '{"error": "timeout", "message": "Connection timed out after 30s"}'
        result = _extract_text_http_info(text)
        assert "Connection timed out" in result

    def test_corr_id_without_error_signal_returns_empty(self):
        text = '{"RqUID": "abc-123"}'
        result = _extract_text_http_info(text)
        assert result == ""

    def test_no_http_signals_returns_empty(self):
        text = "Just a regular log line without any HTTP context"
        result = _extract_text_http_info(text)
        assert result == ""

    def test_fault_code_extracted(self):
        text = '{"faultCode": "SVC0001", "faultString": "Internal error"}'
        result = _extract_text_http_info(text)
        assert "SVC0001" in result


from alla.services.log_extraction_service import _detect_and_extract_http, _scan_json_for_http_info


class TestDetectAndExtractHttp:
    def test_valid_json_uses_scanner(self):
        content = b'{"RqUID": "req-001", "statusCode": 503, "error": "Timeout"}'
        result = _detect_and_extract_http(content, "json")
        assert "req-001" in result
        assert "503" in result
        assert "Timeout" in result

    def test_ndjson_multiple_objects(self):
        content = (
            b'{"RqUID": "r1", "error": "first"}\n'
            b'{"RqUID": "r2", "statusCode": 500}\n'
        )
        result = _detect_and_extract_http(content, "json")
        assert "r1" in result
        assert "r2" in result

    def test_invalid_json_falls_back_to_regex(self):
        content = b'RqUID=fallback-id\nHTTP/1.1 500 Internal Server Error'
        result = _detect_and_extract_http(content, "json")
        assert "fallback-id" in result
        assert "500" in result

    def test_xml_content_uses_regex(self):
        content = b"<RqUID>xml-id</RqUID><faultCode>XML_ERR</faultCode>"
        result = _detect_and_extract_http(content, "xml")
        assert "xml-id" in result
        assert "XML_ERR" in result

    def test_text_content_uses_regex(self):
        content = b'"error": "connection refused"\nRqUID=txt-id'
        result = _detect_and_extract_http(content, "text")
        assert "txt-id" in result
        assert "connection refused" in result

    def test_no_signals_returns_empty(self):
        content = b'{"name": "Alice", "role": "admin"}'
        result = _detect_and_extract_http(content, "json")
        assert result == ""

    def test_json_with_no_http_info_returns_empty(self):
        content = b'{"count": 42, "items": ["a", "b"]}'
        result = _detect_and_extract_http(content, "json")
        assert result == ""

    def test_json_corr_id_without_error_signal_returns_empty(self):
        content = b'{"RqUID": "corr-only"}'
        result = _detect_and_extract_http(content, "json")
        assert result == ""


class TestScanJsonForHttpInfo:
    def test_flat_json_with_all_fields(self):
        obj = {"RqUID": "abc-123", "statusCode": 500, "error": "Service unavailable"}
        result = _scan_json_for_http_info(obj)
        assert "RqUID=abc-123" in result
        assert "500" in result
        assert "Service unavailable" in result

    def test_nested_corr_id(self):
        obj = {"header": {"RqUID": "nested-id", "OperUID": "op-42"}, "body": {}}
        result = _scan_json_for_http_info(obj)
        assert result == ""

    def test_http_status_not_included_if_2xx(self):
        obj = {"statusCode": 200, "message": "OK"}
        result = _scan_json_for_http_info(obj)
        assert result == ""

    def test_http_status_4xx_included(self):
        obj = {"httpStatus": 401, "error": "Unauthorized"}
        result = _scan_json_for_http_info(obj)
        assert "401" in result

    def test_message_not_included_without_error_signal(self):
        obj = {"message": "Transaction completed successfully"}
        result = _scan_json_for_http_info(obj)
        assert result == ""

    def test_message_included_when_error_present(self):
        obj = {"statusCode": 500, "error": "Internal Error", "message": "DB timeout"}
        result = _scan_json_for_http_info(obj)
        assert "DB timeout" in result

    def test_deeply_nested_error(self):
        obj = {
            "response": {
                "header": {"RqUID": "deep-id"},
                "body": {
                    "errors": [
                        {"errorCode": "E001", "errorMessage": "Service down"}
                    ]
                },
            }
        }
        result = _scan_json_for_http_info(obj)
        assert "deep-id" in result
        assert "E001" in result
        assert "Service down" in result

    def test_no_signals_returns_empty(self):
        obj = {"name": "John", "age": 30, "active": True}
        result = _scan_json_for_http_info(obj)
        assert result == ""

    def test_depth_limit_respected(self):
        obj: dict = {"level": 0}
        current = obj
        for i in range(1, 15):
            current["child"] = {"level": i}
            current = current["child"]
        current["RqUID"] = "deep-corr"
        result = _scan_json_for_http_info(obj)
        assert isinstance(result, str)

    def test_list_of_objects(self):
        obj = [
            {"RqUID": "id-1", "error": "first failure"},
            {"RqUID": "id-2", "statusCode": 503},
        ]
        result = _scan_json_for_http_info(obj)
        assert "id-1" in result or "id-2" in result


import logging

import pytest
from unittest.mock import patch

from alla.models.testops import AttachmentMeta, FailedTestSummary
from alla.models.common import TestStatus
from alla.services.log_extraction_service import LogExtractionConfig, LogExtractionService
from conftest import make_failed_test_summary


class FakeAttachmentProvider:
    def __init__(self, attachments: list[AttachmentMeta], content_map: dict[int, bytes]):
        self._attachments = attachments
        self._content_map = content_map

    async def get_attachments_for_test_result(self, test_result_id: int) -> list[AttachmentMeta]:
        return self._attachments

    async def get_attachment_content(self, attachment_id: int) -> bytes:
        return self._content_map[attachment_id]


def make_summary(test_result_id: int = 1) -> FailedTestSummary:
    return FailedTestSummary(test_result_id=test_result_id, name="test_foo", status=TestStatus.FAILED)


class TestLogExtractionServiceIntegration:
    @pytest.mark.asyncio
    async def test_json_attachment_extracts_corr_id_and_error(self):
        content = b'{"RqUID": "req-abc", "statusCode": 503, "error": "Backend unavailable"}'
        att = AttachmentMeta(id=1, name="response.json", type="application/json")
        provider = FakeAttachmentProvider([att], {1: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="application/json"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        assert "req-abc" in summary.log_snippet
        assert "503" in summary.log_snippet
        assert "Backend unavailable" in summary.log_snippet
        assert summary.correlation_hint == "rqUID=req-abc"

    @pytest.mark.asyncio
    async def test_text_attachment_extracts_both_log_and_http(self):
        content = (
            b"2026-01-01 10:00:00 [ERROR] Connection refused\n"
            b'    at Service.java:42\n'
            b'"RqUID": "mixed-id"\n'
            b'"error": "upstream timeout"\n'
        )
        att = AttachmentMeta(id=2, name="test.log", type="text/plain")
        provider = FakeAttachmentProvider([att], {2: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        assert "Connection refused" in summary.log_snippet
        assert "mixed-id" in summary.log_snippet

    @pytest.mark.asyncio
    async def test_binary_attachment_skipped(self):
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        att = AttachmentMeta(id=3, name="screenshot.png", type="image/png")
        provider = FakeAttachmentProvider([att], {3: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="image/png"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is None
        assert summary.correlation_hint is None

    @pytest.mark.asyncio
    async def test_no_processable_attachments_skips_download(self):
        att = AttachmentMeta(id=4, name="data.bin", type="application/octet-stream")
        download_called = []

        class TrackingProvider(FakeAttachmentProvider):
            async def get_attachment_content(self, attachment_id: int) -> bytes:
                download_called.append(attachment_id)
                return b""

        provider = TrackingProvider([att], {})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()
        await service.enrich_with_logs([summary])

        assert len(download_called) == 0
        assert summary.log_snippet is None
        assert summary.correlation_hint is None

    @pytest.mark.asyncio
    async def test_enrich_falls_back_to_status_trace_when_no_processable_attachments(self):
        att = AttachmentMeta(id=40, name="screenshot.png", type="image/png")
        download_called = []

        class TrackingProvider(FakeAttachmentProvider):
            async def get_attachment_content(self, attachment_id: int) -> bytes:
                download_called.append(attachment_id)
                return b""

        provider = TrackingProvider([att], {})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()
        summary.status_trace = "AssertionError: request failed with RqUID=trace-only"

        await service.enrich_with_logs([summary])

        assert len(download_called) == 0
        assert summary.log_snippet is None
        assert summary.correlation_hint == "rqUID=trace-only"

    @pytest.mark.asyncio
    async def test_multiple_json_attachments_combined(self):
        content1 = b'{"RqUID": "first", "error": "step1 failed"}'
        content2 = b'{"RqUID": "second", "statusCode": 500, "error": "step2 failed"}'
        atts = [
            AttachmentMeta(id=10, name="step1.json", type="application/json"),
            AttachmentMeta(id=11, name="step2.json", type="application/json"),
        ]
        provider = FakeAttachmentProvider(atts, {10: content1, 11: content2})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=2))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="application/json"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        assert "first" in summary.log_snippet
        assert "second" in summary.log_snippet

    @pytest.mark.asyncio
    async def test_attachment_name_with_newline_sanitized(self):
        """Имя вложения с переносом строки не ломает формат заголовка секции."""
        content = b'{"RqUID": "id1", "error": "fail"}'
        att = AttachmentMeta(
            id=20, name="Отправлен запрос ->\n", type="application/json"
        )
        provider = FakeAttachmentProvider([att], {20: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="application/json"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        # Заголовок секции должен быть однострочным
        for line in summary.log_snippet.splitlines():
            if line.startswith("---"):
                assert line.strip().endswith("] ---"), (
                    f"Section header broken across lines: {line!r}"
                )

    @pytest.mark.asyncio
    async def test_correlation_only_attachments_do_not_create_log_snippet(self):
        atts = [
            AttachmentMeta(id=30, name="TrRq", type="text/plain"),
            AttachmentMeta(id=31, name="TrRs", type="application/json"),
            AttachmentMeta(id=32, name="DB_LOG", type="text/plain"),
        ]
        provider = FakeAttachmentProvider(
            atts,
            {
                30: b"OperUID=239482348\nRqUID=324234523420",
                31: b'{"OperUID": "239482348"}',
                32: b"OperUID=239482348",
            },
        )
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch(
                 "magic.from_buffer",
                 side_effect=["text/plain", "application/json", "text/plain"],
             ):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is None
        assert summary.correlation_hint == "operUID=239482348, rqUID=324234523420"

    @pytest.mark.asyncio
    async def test_enrich_falls_back_to_status_trace_when_attachments_have_no_correlation(self):
        content = b"2026-01-01 10:00:00 [ERROR] Assertion failed"
        att = AttachmentMeta(id=40, name="test.log", type="text/plain")
        provider = FakeAttachmentProvider([att], {40: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()
        summary.status_trace = "Caused by: HttpError: RqUID=abc-xyz not found"

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.correlation_hint == "rqUID=abc-xyz"

    @pytest.mark.asyncio
    async def test_enrich_prefers_attachment_correlation_over_status_trace(self):
        content = b"OperUID=op-a\nHTTP/1.1 500 Internal Server Error"
        att = AttachmentMeta(id=41, name="response.txt", type="text/plain")
        provider = FakeAttachmentProvider([att], {41: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()
        summary.status_trace = "OperUID=op-b"

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.correlation_hint == "operUID=op-a"

    @pytest.mark.asyncio
    async def test_enrich_keeps_none_when_both_sources_empty(self):
        content = b"2026-01-01 10:00:00 [ERROR] Assertion failed"
        att = AttachmentMeta(id=42, name="test.log", type="text/plain")
        provider = FakeAttachmentProvider([att], {42: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.correlation_hint is None

    @pytest.mark.asyncio
    async def test_structured_journal_attachment_extracted_in_full(self):
        """text/plain вложение с JSON-массивом структурированных лог-записей.

        Должно дать секцию ``--- [журнал: ...] ---`` со всеми объектами и
        полями, без HTTP-секции (handler `consumed=True`).
        """
        import json as _json

        items = [
            {
                "deploymentUnit": "billing-prod",
                "tenantCode": "tenant-42",
                "subsystem": "billing-service",
                "stackTrace": "com.example.Billing.charge(Billing.java:42)\n  at sun.reflect.GeneratedMethodAccessor1.invoke",
                "message": "Failed to charge order #123",
                "logLevel": "ERROR",
                "errorCode": "BILL_15",
                "rqUID": "req-abc-1",
            },
            {
                "deploymentUnit": "notification-prod",
                "tenantCode": "tenant-42",
                "subsystem": "notification",
                "stackTrace": "com.example.Notify.send(Notify.java:7)",
                "message": "Email gateway unreachable",
                "logLevel": "ERROR",
                "errorCode": "NOT_03",
            },
        ]
        content = _json.dumps(items, ensure_ascii=False).encode("utf-8")
        att = AttachmentMeta(id=99, name="journal_dump.txt", type="text/plain")
        provider = FakeAttachmentProvider([att], {99: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_summary()

        # Эвристика _detect_content_type увидит ведущий '[' и переключит
        # text/plain на 'json' — handler примет его как JSON-массив.
        # _MAGIC_AVAILABLE=False гонит через fallback_mime path, чтобы тест
        # не зависел от наличия системной libmagic.
        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", False):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        # Заголовок секции — журнал
        assert "--- [журнал: journal_dump.txt] ---" in summary.log_snippet
        # HTTP-секция для этого же файла НЕ создаётся
        assert "--- [HTTP: journal_dump.txt] ---" not in summary.log_snippet
        # Тело секции — pretty-printed JSON, roundtrip даёт исходный массив.
        body = summary.log_snippet.split("--- [журнал: journal_dump.txt] ---\n", 1)[1]
        assert _json.loads(body) == items
        # correlation hint извлечён из rqUID
        assert summary.correlation_hint == "rqUID=req-abc-1"

    @pytest.mark.asyncio
    async def test_log_snippet_is_truncated_when_exceeds_max_chars(self):
        """``max_snippet_chars`` обрезает финальный log_snippet с маркером."""
        # Лог с большим [ERROR]-блоком, расширяемый stack trace-ом.
        big_trace = "\n".join(
            f"    at com.example.deep.Frame{i}.call(Frame{i}.java:{i})"
            for i in range(500)
        )
        content = (
            f"2026-02-09 10:23:45,123 [ERROR] BoomException\n{big_trace}"
        ).encode("utf-8")
        att = AttachmentMeta(id=11, name="big.log", type="text/plain")
        provider = FakeAttachmentProvider([att], {11: content})
        service = LogExtractionService(
            provider,
            LogExtractionConfig(concurrency=1, max_snippet_chars=500),
        )
        summary = make_summary()

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.log_snippet is not None
        # Полная склейка ушла бы в десятки тысяч символов; обрезка должна
        # удержать тело в пределах max_chars + длина маркера.
        assert "обрезано" in summary.log_snippet
        # Сам маркер фиксированной длины; основное тело должно начинаться с
        # 500 символов оригинального текста.
        assert summary.log_snippet.startswith("--- [")
        body, _, marker = summary.log_snippet.partition("\n\n[... обрезано")
        assert len(body) == 500
        assert marker.endswith("...]")


class TestDetailsAttachmentAugmentation:
    """Details-вложение Expected/Actual поднимается в status_message."""

    @pytest.mark.asyncio
    async def test_details_appended_to_existing_status_message(self):
        details = "Expected: <200>\nActual: <500>"
        att = AttachmentMeta(id=101, name="Details", type="text/plain")
        provider = FakeAttachmentProvider([att], {101: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\n\n{details}"
        assert summary.log_snippet is None

    @pytest.mark.asyncio
    async def test_details_becomes_status_message_when_empty(self):
        details = "Expected: success\r\nActual: failure"
        att = AttachmentMeta(id=102, name="Details.txt", type="text/plain")
        provider = FakeAttachmentProvider([att], {102: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message=None)

        await service.enrich_with_logs([summary])

        assert summary.status_message == "Expected: success\nActual: failure"
        assert summary.log_snippet is None

    @pytest.mark.asyncio
    async def test_no_details_keeps_status_message_and_runs_handlers(self):
        content = b"2026-01-01 10:00:00 [ERROR] backend failed"
        att = AttachmentMeta(id=103, name="test.log", type="text/plain")
        provider = FakeAttachmentProvider([att], {103: content})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.status_message == "AssertionError"
        assert summary.log_snippet is not None
        assert "backend failed" in summary.log_snippet

    @pytest.mark.parametrize(
        ("name", "mime", "matches"),
        [
            ("Details", "text/plain", True),
            ("DETAILS", "text/plain", True),
            ("details.txt", "text/plain", True),
            ("Details.log", "text/plain", True),
            ("Details", "", True),
            ("assertion_details.txt", "text/plain", False),
            ("my_details", "text/plain", False),
            ("details.json", "text/plain", False),
            ("Details.png", "text/plain", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_details_name_and_mime_matrix(self, name, mime, matches):
        details = "Expected: <a>\nActual: <b>"
        att = AttachmentMeta(id=104, name=name, type=mime)
        provider = FakeAttachmentProvider([att], {104: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        await service.enrich_with_logs([summary])

        if matches:
            assert summary.status_message == f"AssertionError\n\n{details}"
        else:
            assert summary.status_message == "AssertionError"

    @pytest.mark.asyncio
    async def test_details_accepts_content_type_when_type_is_missing(self):
        details = "Expected: <left>\nActual: <right>"
        att = AttachmentMeta(
            id=114,
            name="Details",
            type=None,
            content_type="text/plain",
        )
        provider = FakeAttachmentProvider([att], {114: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\n\n{details}"

    @pytest.mark.asyncio
    async def test_details_with_non_text_mime_is_not_augmented(self):
        att = AttachmentMeta(id=105, name="Details", type="image/png")
        provider = FakeAttachmentProvider([att], {105: b"Expected: a\nActual: b"})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        await service.enrich_with_logs([summary])

        assert summary.status_message == "AssertionError"

    @pytest.mark.asyncio
    async def test_multiple_details_uses_first_and_logs_skipped_rest(self, caplog):
        first_details = "Expected: <first>\nActual: <used>"
        second_details = (
            "Expected: <second>\nActual: <skipped>\n"
            "2026-01-01 10:00:00 [ERROR] should not become log"
        )
        atts = [
            AttachmentMeta(id=115, name="Details", type="text/plain"),
            AttachmentMeta(id=116, name="Details.txt", type="text/plain"),
        ]
        provider = FakeAttachmentProvider(
            atts,
            {
                115: first_details.encode("utf-8"),
                116: second_details.encode("utf-8"),
            },
        )
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")
        caplog.set_level(logging.DEBUG, logger="alla.services.log_extraction_service")

        await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\n\n{first_details}"
        assert summary.log_snippet is None
        assert "пропущено дополнительных Details" in caplog.text

    @pytest.mark.asyncio
    async def test_large_details_is_truncated_with_marker(self):
        details = "Expected: <ok>\nActual: " + ("x" * 5000)
        att = AttachmentMeta(id=106, name="Details", type="text/plain")
        provider = FakeAttachmentProvider([att], {106: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        await service.enrich_with_logs([summary])

        assert summary.status_message is not None
        appended = summary.status_message.split("\n\n", 1)[1]
        assert appended.endswith("\n... [Details обрезаны]")
        assert len(appended) == 4096 + len("\n... [Details обрезаны]")

    @pytest.mark.asyncio
    async def test_details_is_idempotent_when_already_inline(self):
        details = "Expected: <ok>\nActual: <fail>"
        att = AttachmentMeta(id=107, name="Details", type="text/plain")
        provider = FakeAttachmentProvider([att], {107: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(
            status_message=f"AssertionError\n\n{details}",
        )

        await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\n\n{details}"

    @pytest.mark.asyncio
    async def test_details_is_idempotent_with_different_newline_style(self):
        details = "Expected: <ok>\nActual: <fail>"
        inline_details = "Expected: <ok>\r\nActual: <fail>"
        att = AttachmentMeta(id=113, name="Details", type="text/plain")
        provider = FakeAttachmentProvider([att], {113: details.encode("utf-8")})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(
            status_message=f"AssertionError\r\n\r\n{inline_details}",
        )

        await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\r\n\r\n{inline_details}"

    @pytest.mark.asyncio
    async def test_details_and_neighbor_log_are_split_between_channels(self):
        details = "Expected: <active>\nActual: <blocked>"
        log = b"2026-01-01 10:00:00 [ERROR] Service unavailable"
        atts = [
            AttachmentMeta(id=108, name="Details", type="text/plain"),
            AttachmentMeta(id=109, name="test.log", type="text/plain"),
        ]
        provider = FakeAttachmentProvider(
            atts,
            {108: details.encode("utf-8"), 109: log},
        )
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.status_message == f"AssertionError\n\n{details}"
        assert summary.log_snippet is not None
        assert "Service unavailable" in summary.log_snippet
        assert "Expected: <active>" not in summary.log_snippet

    @pytest.mark.asyncio
    async def test_details_download_error_does_not_stop_pipeline(self):
        details_att = AttachmentMeta(id=110, name="Details", type="text/plain")
        log_att = AttachmentMeta(id=111, name="test.log", type="text/plain")

        class FailingDetailsProvider(FakeAttachmentProvider):
            async def get_attachment_content(self, attachment_id: int) -> bytes:
                if attachment_id == 110:
                    raise RuntimeError("download failed")
                return await super().get_attachment_content(attachment_id)

        provider = FailingDetailsProvider(
            [details_att, log_att],
            {111: b"2026-01-01 10:00:00 [ERROR] downstream failed"},
        )
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        with patch("alla.services.log_extraction_service._MAGIC_AVAILABLE", True), \
             patch("magic.from_buffer", return_value="text/plain"):
            await service.enrich_with_logs([summary])

        assert summary.status_message == "AssertionError"
        assert summary.log_snippet is not None
        assert "downstream failed" in summary.log_snippet

    @pytest.mark.asyncio
    async def test_binary_garbage_details_is_skipped(self):
        att = AttachmentMeta(id=112, name="Details.txt", type="text/plain")
        provider = FakeAttachmentProvider([att], {112: bytes(range(256)) * 4})
        service = LogExtractionService(provider, LogExtractionConfig(concurrency=1))
        summary = make_failed_test_summary(status_message="AssertionError")

        with patch("alla.services.log_extraction_service._decode_text", return_value=None):
            await service.enrich_with_logs([summary])

        assert summary.status_message == "AssertionError"
