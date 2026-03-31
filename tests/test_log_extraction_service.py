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

    def test_no_http_signals_returns_empty(self):
        text = "Just a regular log line without any HTTP context"
        result = _extract_text_http_info(text)
        assert result == ""

    def test_fault_code_extracted(self):
        text = '{"faultCode": "SVC0001", "faultString": "Internal error"}'
        result = _extract_text_http_info(text)
        assert "SVC0001" in result
