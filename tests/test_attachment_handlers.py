"""Тесты для StructuredErrorLogHandler и реестра обработчиков вложений."""

import json

from alla.models.testops import AttachmentMeta
from alla.services.attachment_handlers import (
    AttachmentContext,
    ErrorBlocksHandler,
    HttpSignalsHandler,
    StructuredErrorLogHandler,
    default_handlers,
)


def _ctx(content: bytes, *, detected_type: str = "json", name: str = "log.txt") -> AttachmentContext:
    decoded = content.decode("utf-8", errors="replace")
    return AttachmentContext(
        att=AttachmentMeta(id=1, name=name, type="text/plain"),
        content=content,
        detected_type=detected_type,
        decoded_text=decoded,
    )


class TestStructuredErrorLogHandlerDetection:
    def test_detects_structured_journal(self) -> None:
        items = [
            {
                "subsystem": "auth",
                "stackTrace": "com.example.Auth.fail()",
                "message": "Login failed",
                "logLevel": "ERROR",
            },
            {
                "subsystem": "db",
                "message": "Timeout",
                "logLevel": "ERROR",
                "errorCode": "DB_42",
            },
        ]
        content = json.dumps(items, ensure_ascii=False).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        assert result.label == "журнал"
        assert result.consumed is True
        assert "subsystem=auth" in result.section
        assert "Login failed" in result.section
        assert "DB_42" in result.section

    def test_rejects_plain_array_of_unrelated_objects(self) -> None:
        items = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        content = json.dumps(items).encode("utf-8")
        assert StructuredErrorLogHandler().handle(_ctx(content)) is None

    def test_rejects_single_dict(self) -> None:
        content = json.dumps(
            {"message": "x", "logLevel": "ERROR", "stackTrace": "..."}
        ).encode("utf-8")
        assert StructuredErrorLogHandler().handle(_ctx(content)) is None

    def test_rejects_empty_array(self) -> None:
        assert StructuredErrorLogHandler().handle(_ctx(b"[]")) is None

    def test_rejects_invalid_json(self) -> None:
        assert StructuredErrorLogHandler().handle(_ctx(b"not json at all")) is None

    def test_rejects_xml_detected_type(self) -> None:
        items = [{"message": "x", "logLevel": "ERROR", "stackTrace": "..."}]
        content = json.dumps(items).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content, detected_type="xml"))
        assert result is None

    def test_rejects_binary_detected_type(self) -> None:
        items = [{"message": "x", "logLevel": "ERROR", "stackTrace": "..."}]
        content = json.dumps(items).encode("utf-8")
        assert (
            StructuredErrorLogHandler().handle(_ctx(content, detected_type="binary"))
            is None
        )

    def test_majority_threshold_60_percent(self) -> None:
        """3 из 5 объектов — лог-записи (60%), handler должен сработать."""
        items = [
            {"message": "fail", "logLevel": "ERROR"},
            {"message": "fail", "stackTrace": "..."},
            {"subsystem": "x", "errorCode": "E1"},
            {"unrelated": "data"},
            {"more": "junk"},
        ]
        content = json.dumps(items).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None

    def test_below_threshold_rejected(self) -> None:
        """2 из 5 — ниже 60%, не срабатываем."""
        items = [
            {"message": "fail", "logLevel": "ERROR"},
            {"message": "fail", "stackTrace": "..."},
            {"unrelated": "data"},
            {"foo": "bar"},
            {"baz": "qux"},
        ]
        content = json.dumps(items).encode("utf-8")
        assert StructuredErrorLogHandler().handle(_ctx(content)) is None


class TestStructuredErrorLogHandlerFormatting:
    def test_section_includes_all_keys_of_all_objects(self) -> None:
        items = [
            {
                "subsystem": "auth",
                "logLevel": "ERROR",
                "message": "Login failed",
                "stackTrace": "com.example.Auth.fail()\n  at line 42",
                "customField": "extra-value",
            },
            {
                "subsystem": "db",
                "logLevel": "WARN",
                "message": "Slow query",
                "stackTrace": "com.example.Db.q()",
            },
        ]
        content = json.dumps(items, ensure_ascii=False).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        section = result.section
        assert "[#1]" in section
        assert "[#2]" in section
        assert "logLevel=ERROR" in section
        assert "logLevel=WARN" in section
        assert "subsystem=auth" in section
        assert "subsystem=db" in section
        assert "Login failed" in section
        assert "Slow query" in section
        assert "com.example.Auth.fail()" in section
        assert "  at line 42" in section
        assert "customField: extra-value" in section

    def test_correlation_hint_extracted(self) -> None:
        items = [
            {
                "message": "fail",
                "logLevel": "ERROR",
                "stackTrace": "...",
                "rqUID": "req-abc-123",
                "operUID": "op-456",
            }
        ]
        content = json.dumps(items).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        assert result.correlation_hint == "operUID=op-456, rqUID=req-abc-123"

    def test_correlation_hint_extracted_from_nested_object(self) -> None:
        """Correlation IDs во вложенных dict тоже находятся (как в общем JSON-сканере)."""
        items = [
            {
                "message": "fail",
                "logLevel": "ERROR",
                "stackTrace": "...",
                "context": {
                    "request": {"rqUID": "deep-req-1", "operUID": "deep-op-1"},
                },
            }
        ]
        content = json.dumps(items).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        assert result.correlation_hint == "operUID=deep-op-1, rqUID=deep-req-1"

    def test_correlation_hint_none_when_absent(self) -> None:
        items = [
            {"message": "fail", "logLevel": "ERROR", "stackTrace": "..."},
            {"message": "fail2", "logLevel": "ERROR", "stackTrace": "..."},
        ]
        content = json.dumps(items).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        assert result.correlation_hint is None

    def test_large_journal_not_truncated(self) -> None:
        """Файл ~12 000 символов целиком попадает в секцию."""
        items = []
        for i in range(40):
            items.append({
                "subsystem": f"svc-{i}",
                "logLevel": "ERROR",
                "message": f"failure number {i} with reasonably long description " * 4,
                "stackTrace": "com.example.Class.method()\n  at line 1\n  at line 2\n" * 3,
            })
        content = json.dumps(items, ensure_ascii=False).encode("utf-8")
        assert len(content) >= 10_000
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        # Все 40 объектов должны присутствовать
        for i in range(40):
            assert f"svc-{i}" in result.section
            assert f"failure number {i}" in result.section

    def test_nested_object_serialized_as_json(self) -> None:
        items = [
            {
                "message": "fail",
                "logLevel": "ERROR",
                "stackTrace": "...",
                "context": {"userId": 42, "tags": ["a", "b"]},
            }
        ]
        content = json.dumps(items, ensure_ascii=False).encode("utf-8")
        result = StructuredErrorLogHandler().handle(_ctx(content))
        assert result is not None
        assert "userId" in result.section
        assert '"a"' in result.section or "'a'" in result.section


class TestStructuredErrorLogHandlerNameAgnostic:
    """Имя файла НЕ должно влиять на детект — только содержимое."""

    def test_works_with_arbitrary_name(self) -> None:
        items = [
            {"message": "fail", "logLevel": "ERROR", "stackTrace": "..."},
            {"message": "fail2", "logLevel": "ERROR", "stackTrace": "..."},
        ]
        content = json.dumps(items).encode("utf-8")
        for name in ("random.txt", "data.json", "Какой-то лог", "x.log", ""):
            ctx = _ctx(content, name=name)
            result = StructuredErrorLogHandler().handle(ctx)
            assert result is not None, f"failed for name={name!r}"

    def test_rejects_unrelated_array_regardless_of_name(self) -> None:
        items = [{"foo": 1}, {"bar": 2}]
        content = json.dumps(items).encode("utf-8")
        for name in ("Ошибка из журнала.txt", "log.txt", "error.json"):
            ctx = _ctx(content, name=name)
            assert StructuredErrorLogHandler().handle(ctx) is None


class TestErrorBlocksHandler:
    def test_extracts_error_blocks(self) -> None:
        text = (
            "2026-01-01 10:00:00 [ERROR] Boom\n"
            "    at Foo.bar(Foo.java:1)\n"
            "2026-01-01 10:00:01 [INFO] done"
        )
        ctx = AttachmentContext(
            att=AttachmentMeta(id=1, name="app.log", type="text/plain"),
            content=text.encode("utf-8"),
            detected_type="text",
            decoded_text=text,
        )
        result = ErrorBlocksHandler().handle(ctx)
        assert result is not None
        assert result.label == "файл"
        assert result.consumed is False
        assert "Boom" in result.section
        assert "[INFO]" not in result.section

    def test_skips_non_text(self) -> None:
        ctx = AttachmentContext(
            att=AttachmentMeta(id=1, name="x.json", type="application/json"),
            content=b'{"x":1}',
            detected_type="json",
            decoded_text='{"x":1}',
        )
        assert ErrorBlocksHandler().handle(ctx) is None


class TestHttpSignalsHandler:
    def test_extracts_http_section(self) -> None:
        content = b'{"RqUID": "abc", "statusCode": 503, "error": "down"}'
        ctx = AttachmentContext(
            att=AttachmentMeta(id=1, name="r.json", type="application/json"),
            content=content,
            detected_type="json",
            decoded_text=content.decode("utf-8"),
        )
        result = HttpSignalsHandler().handle(ctx)
        assert result is not None
        assert result.label == "HTTP"
        assert result.consumed is False
        assert "503" in result.section
        assert "abc" in (result.correlation_hint or "")


class TestDefaultHandlersRegistry:
    def test_default_order_by_priority(self) -> None:
        handlers = default_handlers()
        priorities = [h.priority for h in handlers]
        assert priorities == sorted(priorities)
        names = [h.name for h in handlers]
        # Структурированный журнал должен идти первым
        assert names[0] == "structured-error-log"
