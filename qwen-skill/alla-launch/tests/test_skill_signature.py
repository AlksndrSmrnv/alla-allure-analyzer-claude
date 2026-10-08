"""Сигнатура проблемы: что входит в материал и когда в него попадает лог."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.models.testops import AttachmentMeta, FailedTestSummary
from alla_core.services.attachment_handlers import AttachmentContext, StructuredErrorLogHandler
from alla_core.services.log_extraction_service import LogExtractionConfig, LogExtractionService
from alla_skill_lib.signature import (
    SIGNATURE_VERSION,
    cluster_signature,
    is_generic_assertion,
    signature_material,
)
from eval.corpus_dev import ASSERT_500, CATALOG_NPE_LOG, PAYMENT_POOL_LOG, _assert_trace
from skill_factories import make_single_test_cluster

ASSERT_TRACE = _assert_trace(ASSERT_500, "ru.company.payments.PaymentTest", "payByCard", 30)
TIMEOUT = "java.net.SocketTimeoutException: Read timed out"
TIMEOUT_TRACE = TIMEOUT + "\n\tat ru.company.payments.PaymentClient.refund(PaymentClient.java:19)\n"
CAUSED_TRACE = (
    f"java.lang.AssertionError: {ASSERT_500}\n"
    "\tat ru.company.payments.PaymentTest.payByCard(PaymentTest.java:30)\n"
    "Caused by: java.lang.IllegalStateException: payment client is closed\n"
    "\t... 3 more\n"
)
INFO_LOG = (
    "2026-10-03 10:00:00 [INFO] PaymentController: POST /payments\n"
    "2026-10-03 10:00:01 [INFO] PaymentController: POST /payments -> 500\n"
)


def _signature(message: str, trace: str, log: str, test_id: int = 1) -> str | None:
    return cluster_signature(*make_single_test_cluster(message, trace, log, test_id))


def _material(message: str, trace: str, log: str) -> str | None:
    return signature_material(*make_single_test_cluster(message, trace, log))


def _pool_log(time: str, thread: str, request: str, order: int) -> str:
    return (
        f"{time} [ERROR] [{thread}] PaymentRepository: could not load payment {order} "
        f"for request {request}\n"
        "java.sql.SQLTransientConnectionException: HikariPool-2 - Connection is not available, "
        "request timed out after 30000ms.\n"
        "\tat ru.company.payments.PaymentRepository.find(PaymentRepository.java:57)\n"
    )


def test_signature_has_the_current_version() -> None:
    assert str(_signature(ASSERT_500, ASSERT_TRACE, "")).startswith(f"v{SIGNATURE_VERSION}:")
    assert SIGNATURE_VERSION == 7


def test_same_generic_assertion_with_different_log_errors_differs() -> None:
    pool = _signature(ASSERT_500, ASSERT_TRACE, PAYMENT_POOL_LOG)
    npe = _signature(ASSERT_500, ASSERT_TRACE, CATALOG_NPE_LOG)

    assert pool != npe
    material = _material(ASSERT_500, ASSERT_TRACE, PAYMENT_POOL_LOG)
    assert material is not None and material.startswith("message+trace+log\n")
    assert "hikaripool-2 - connection is not available" in material


def test_same_log_error_with_other_time_ids_and_threads_is_one_signature() -> None:
    first = _pool_log("2026-10-03 10:00:30", "http-nio-8080-exec-7",
                      "0f8a1c2e-1b2c-4d5e-8f90-123456789abc", 84736251)
    second = _pool_log("2026-10-05 23:59:59.123", "pool-3-thread-12",
                       "11111111-2222-3333-4444-555555555555", 11112222)

    assert (_signature(ASSERT_500, ASSERT_TRACE, first)
            == _signature(ASSERT_500, ASSERT_TRACE, second, test_id=987654))


def test_generic_assertion_ignores_a_log_of_only_info_lines() -> None:
    without_log = _signature(ASSERT_500, ASSERT_TRACE, "")

    assert _signature(ASSERT_500, ASSERT_TRACE, INFO_LOG) == without_log
    assert _signature(ASSERT_500, ASSERT_TRACE, INFO_LOG.replace("POST", "GET")) == without_log


@pytest.mark.parametrize("level", ["ALERT", "ERR", "CRIT", "EMERG", "ERROR"])
def test_every_error_level_of_the_log_extraction_names_the_cause(level: str) -> None:
    def log(text: str) -> str:
        return f"2026-10-03 10:00:00 [{level}] {text}\n"

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("database unavailable"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("disk full")))


@pytest.mark.parametrize("line", [
    "2026-10-03 10:00:00 [alert] {text}",
    "2026-10-03 10:00:00 [err] {text}",
    "2026-10-03 10:00:00 [Alert] {text}",
    'time=2026-10-03T10:00:00Z level=crit msg="{text}"',
])
def test_level_is_read_by_its_position_in_any_case(line: str) -> None:
    def log(text: str) -> str:
        return line.format(text=text) + "\n"

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("database unavailable"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("disk full")))


@pytest.mark.parametrize("level", ["ALERT", "ERR", "CRIT", "EMERG", "SEVERE"])
def test_jul_level_line_under_the_heading_names_the_cause(level: str) -> None:
    def log(text: str) -> str:
        return (f"Oct 03, 2026 10:00:00 AM ru.company.payments.PaymentRepository find\n"
                f"{level}: {text}\n")

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("database unavailable"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("disk full")))
    material = _material(ASSERT_500, ASSERT_TRACE, log("database unavailable"))
    assert material is not None and f"{level.lower()}: database unavailable" in material


def test_lowercase_level_words_in_info_lines_are_not_errors() -> None:
    noise = "2026-10-03 10:00:00 [INFO] alert sent, err counter reset\n"

    assert _signature(ASSERT_500, ASSERT_TRACE, noise) == _signature(ASSERT_500, ASSERT_TRACE, "")


def _journal(*records: dict[str, Any]) -> str:
    """Секция журнала, как её строит извлечение лога (JSON с отступами)."""
    entries = [{"deploymentUnit": "billing-prod", "tenantCode": "tenant-42", **record}
               for record in records]
    content = json.dumps(entries).encode()
    result = StructuredErrorLogHandler().handle(AttachmentContext(
        att=AttachmentMeta(id=1, name="journal.json", type="application/json"),
        content=content, detected_type="json", decoded_text=content.decode()))
    assert result is not None
    return f"--- [{result.label}: journal.json] ---\n{result.section}"


def test_journal_record_keeps_its_level_with_its_message() -> None:
    def journal(message: str, request: str) -> str:
        return _journal(
            {"logLevel": "INFO", "message": "POST /payments", "rqUID": request},
            {"logLevel": "ERROR", "message": message, "rqUID": request,
             "stackTrace": "com.example.Billing.charge(Billing.java:42)"},
        )

    pool = _signature(ASSERT_500, ASSERT_TRACE, journal("connection pool exhausted", "req-1"))

    assert pool != _signature(ASSERT_500, ASSERT_TRACE, journal("email gateway unreachable", "req-1"))
    assert pool == _signature(ASSERT_500, ASSERT_TRACE, journal("connection pool exhausted", "req-2"))
    material = _material(ASSERT_500, ASSERT_TRACE, journal("connection pool exhausted", "req-1"))
    assert material is not None and "[error] connection pool exhausted" in material
    assert "post /payments" not in material


@pytest.mark.parametrize("field", ["errorCode", "error_code", "code"])
def test_journal_error_codes_keep_their_field_name(field: str) -> None:
    def journal(code: Any) -> str:
        return _journal({"logLevel": "ERROR", "message": "gateway rejected payment", field: code})

    assert (_signature(ASSERT_500, ASSERT_TRACE, journal(10001))
            != _signature(ASSERT_500, ASSERT_TRACE, journal(10002)))
    assert (_signature(ASSERT_500, ASSERT_TRACE, journal("10001"))
            != _signature(ASSERT_500, ASSERT_TRACE, journal("10002")))


def test_journal_of_only_info_records_is_not_added() -> None:
    journal = _journal({"logLevel": "INFO", "message": "POST /payments"},
                       {"level": "debug", "message": "pool stats"})

    assert _signature(ASSERT_500, ASSERT_TRACE, journal) == _signature(ASSERT_500, ASSERT_TRACE, "")


def test_cut_journal_still_reads_its_records() -> None:
    journal = _journal({"logLevel": "ERROR", "message": "connection pool exhausted",
                        "details": {"pool": "HikariPool-2"}},
                       {"logLevel": "INFO", "message": "retry"})
    cut = journal[:journal.index('"retry"')]

    material = _material(ASSERT_500, ASSERT_TRACE, cut)
    assert material is not None and "[error] connection pool exhausted" in material


def test_record_without_its_braces_is_read_by_its_fields() -> None:
    """Окна отбора без скобок записи: поля соседних записей не смешиваются."""
    lines = _journal({"logLevel": "INFO", "message": "POST /payments"},
                     {"logLevel": "ERROR", "message": "connection pool exhausted"},
                     {"logLevel": "INFO", "message": "retry"}).splitlines()
    windows = "\n".join(line for line in lines
                        if line.strip() not in ("{", "},", "}") and "deploymentUnit" not in line)

    material = _material(ASSERT_500, ASSERT_TRACE, windows)
    assert material is not None and material.endswith("---\n[error] connection pool exhausted")


class _Journal:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def get_attachments_for_test_result(self, _test_id: int) -> list[AttachmentMeta]:
        return [AttachmentMeta(id=100, name="journal.json", type="application/json")]

    async def get_attachment_content(self, _attachment_id: int) -> bytes:
        return self.content


def test_error_record_survives_the_log_selection_of_a_big_journal() -> None:
    def entry(index: int, level: str, message: str) -> dict[str, Any]:
        return {"deploymentUnit": "billing-prod", "tenantCode": "t", "logLevel": level,
                "message": message, "rqUID": f"req-{index}", "details": {"attempt": index}}

    def signature(message: str) -> tuple[str | None, str | None]:
        items = [entry(i, "INFO", f"heartbeat {i} " + "x" * 80) for i in range(200)]
        items.insert(120, entry(120, "ERROR", message))
        summary = FailedTestSummary(test_result_id=1, name="t", status="failed",
                                    status_message=ASSERT_500, status_trace=ASSERT_TRACE)
        service = LogExtractionService(_Journal(json.dumps(items).encode()),
                                       LogExtractionConfig(max_snippet_chars=3000))
        asyncio.run(service.enrich_with_logs([summary]))
        assert summary.log_selection_truncated and '"logLevel": "ERROR"' in summary.log_snippet
        cluster, tests = make_single_test_cluster(ASSERT_500, ASSERT_TRACE)
        tests[1] = summary
        return cluster_signature(cluster, tests), signature_material(cluster, tests)

    pool, material = signature("connection pool exhausted")
    assert material is not None and "[error] connection pool exhausted" in material
    assert pool != signature("email gateway unreachable")[0]


@pytest.mark.parametrize(("message", "trace"), [
    (ASSERT_500, CAUSED_TRACE),
    (TIMEOUT, TIMEOUT_TRACE),
])
def test_error_that_names_the_cause_keeps_the_log_out(message: str, trace: str) -> None:
    pool = _signature(message, trace, PAYMENT_POOL_LOG)

    assert pool == _signature(message, trace, CATALOG_NPE_LOG) == _signature(message, trace, "")
    material = _material(message, trace, PAYMENT_POOL_LOG)
    assert material is not None and material.startswith("message+trace\n")


def test_without_trace_the_log_is_the_material() -> None:
    pool = _signature("request failed", "", PAYMENT_POOL_LOG)

    assert pool != _signature("request failed", "", CATALOG_NPE_LOG)
    assert _signature("request failed", "", INFO_LOG) != _signature("request failed", "", "")
    material = _material("request failed", "", PAYMENT_POOL_LOG)
    assert material is not None and material.startswith("message+log\n")


@pytest.mark.parametrize(("message", "trace"), [
    (ASSERT_500, ASSERT_TRACE),
    ("Order not found", "java.lang.AssertionError: Order not found\n\tat a.B.c(B.java:1)"),
    ("", "kotlin.AssertionError: Expected value to be true.\n\tat a.B.c(B.kt:1)"),
    ("expected: <200> but was: <500>",
     "org.opentest4j.AssertionFailedError: expected: <200> but was: <500>\n\tat a.B.c(B.java:1)"),
    ("", "org.junit.ComparisonFailure: expected:<[OK]> but was:<[FAIL]>"),
    ("", "junit.framework.AssertionFailedError: status\n\tat a.B.c(B.java:1)"),
    ("", "junit.framework.ComparisonFailure: null expected:<a> but was:<b>"),
    ("", "org.assertj.core.error.AssertJMultipleFailuresError: Multiple Failures (2 failures)"),
    ("\nExpected: is <200>\n     but: was <500>", "java.lang.AssertionError: \nExpected: is <200>"
     "\n     but: was <500>\n\tat org.hamcrest.MatcherAssert.assertThat(MatcherAssert.java:20)"),
    ("Status expected:<200> but was:<500>", ""),
    ("expected [200] but found [500]", "java.lang.AssertionError: expected [200] but found [500]"),
    ("AssertionError: assert 500 == 200",
     "    def test_pay(client):\n>       assert client.pay().status == 200\n"
     "E       assert 500 == 200\n\ntests/test_pay.py:12: AssertionError"),
    ("", 'Traceback (most recent call last):\n  File "t.py", line 3, in test\n'
         "    assert status == 200\nAssertionError"),
])
def test_generic_assertions(message: str, trace: str) -> None:
    assert is_generic_assertion(message, trace)


@pytest.mark.parametrize(("message", "trace"), [
    (TIMEOUT, TIMEOUT_TRACE),
    (ASSERT_500, CAUSED_TRACE),
    ("Element not found {#login-form}\nExpected: visible\nTimeout: 4 s.",
     "com.codeborne.selenide.ex.ElementNotFound: Element not found {#login-form}\n"
     "Expected: visible\nTimeout: 4 s.\n\tat ru.company.ui.LoginPage.open(LoginPage.java:22)"),
    ("missing order", "java.lang.IllegalStateException: missing order\n\tat a.B.c(B.java:1)"),
    ("HTTP 500 Internal Server Error", ""),
    ("requests.exceptions.ConnectionError: refused",
     "    def test_pay(client):\n>       assert client.pay().status == 200\n"
     "E       requests.exceptions.ConnectionError: refused\n\ntests/test_pay.py:12: ConnectionError"),
])
def test_errors_that_are_not_generic_assertions(message: str, trace: str) -> None:
    assert not is_generic_assertion(message, trace)
