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
from alla_core.utils.message_resources import message_resources
from alla_skill_lib.signature import (
    SIGNATURE_VERSION,
    cluster_signature,
    is_generic_assertion,
    is_symptom_exception,
    log_names_cause,
    signature_material,
)
from eval.corpus_dev import ASSERT_500, CATALOG_NPE_LOG, PAYMENT_POOL_LOG, _assert_trace
from skill_factories import make_error_log, make_single_test_cluster

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
    assert SIGNATURE_VERSION == 10


@pytest.mark.parametrize("line", [
    "E, [2026-10-03T10:58:00.180456 #{pid}] ERROR -- : [req-9] Orders::CreateJob failed: "
    "warehouse WH-9 has no slot",
    "2026-10-04 11:00:00.731 UTC [{pid}] ERROR:  deadlock detected",
])
def test_process_id_in_the_record_header_does_not_change_the_signature(line: str) -> None:
    """Перезапуск сервиса меняет PID, а не ошибку (ревью Codex)."""
    first = _signature(ASSERT_500, ASSERT_TRACE, line.format(pid=54321) + "\n")
    assert first == _signature(ASSERT_500, ASSERT_TRACE, line.format(pid=54322) + "\n")
    assert first != _signature(ASSERT_500, ASSERT_TRACE,
                               line.format(pid=54321).replace("failed", "rejected")
                               .replace("deadlock", "lock timeout") + "\n")


@pytest.mark.parametrize("level", ["ERROR", "FINE", "INFO", "WARN", "CRIT", "ERR"])
def test_bracketed_number_after_the_level_is_not_a_process_id(level: str) -> None:
    log = (f"2026-10-04 11:00:00,731 {level} [{{n}}] Billing - "
           'errorMessage="tariff rejected"\n')
    assert _signature(ASSERT_500, ASSERT_TRACE, log.format(n=12)) != _signature(
        ASSERT_500, ASSERT_TRACE, log.format(n=13))


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


@pytest.mark.parametrize("field", ["[operUID={oper}]", 'rqUID="{oper}"', "<traceId>{oper}</traceId>"])
def test_operation_id_does_not_change_the_signature(field: str) -> None:
    def log(oper: str, error: str = "account is closed") -> str:
        return (f"2026-10-03 12:00:00 [ERROR] {field.format(oper=oper)} c.e.AccountClient - "
                f"transfer rejected: {error}\n")

    one = _signature(ASSERT_500, ASSERT_TRACE, log("op-tr-3001"))
    material = _material(ASSERT_500, ASSERT_TRACE, log("op-tr-3001"))

    assert one == _signature(ASSERT_500, ASSERT_TRACE, log("op-tr-3002"), test_id=2)
    assert one != _signature(ASSERT_500, ASSERT_TRACE, log("op-tr-3001", "limit exceeded"))
    assert material is not None and "<id>" in material and "<codes:" not in material


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


def test_info_line_with_error_fields_names_the_cause() -> None:
    def log(message: str) -> str:
        return ("2026-10-16 10:00:00 [INFO] c.e.Transfer - accepted\n"
                "2026-10-16 10:00:01 [INFO] c.e.AccountClient - Response "
                f'{{"status":"REJECTED","errorCode":6,"errorMessage":"{message}"}}\n')

    closed = _signature(ASSERT_500, ASSERT_TRACE, log("account is closed"))

    assert closed != _signature(ASSERT_500, ASSERT_TRACE, log("not enough funds"))
    assert closed != _signature(ASSERT_500, ASSERT_TRACE, "")
    material = _material(ASSERT_500, ASSERT_TRACE, log("account is closed"))
    assert material is not None and "account is closed" in material
    assert "accepted" not in material


def _response_log(fields: str) -> str:
    return ("2026-10-16 10:00:01 [INFO] [operUID=op-tr-3001] c.e.AccountClient - Response "
            f'{{"status":"REJECTED",{fields}}}\n')


@pytest.mark.parametrize("account", ['"account":"{n}"', "'account': '{n}'", '"ids":["{n}"]'])
def test_quoted_long_number_in_a_log_line_is_an_id(account: str) -> None:
    def log(number: str) -> str:
        return _response_log('"errorCode":6,"errorMessage":"account is closed",'
                             + account.format(n=number))

    one = _signature(ASSERT_500, ASSERT_TRACE, log("40817810000000000301"))
    material = _material(ASSERT_500, ASSERT_TRACE, log("40817810000000000301"))

    assert one == _signature(ASSERT_500, ASSERT_TRACE, log("40817810000000000302"), test_id=2)
    assert material is not None and "4081" not in material and "<num>" in material
    # Короткий код остаётся в тексте строки, отпечаток его не повторяет: хэш прежних записей.
    assert "<codes:" not in material


@pytest.mark.parametrize("field", ['"errorCode":"{c}"', '"errorCode":{c}', 'errorCode={c}',
                                   '"faultCode":"{c}"', '"error_code":"{c}"'])
def test_long_response_error_code_in_a_log_line_still_differs(field: str) -> None:
    def log(code: str) -> str:
        return _response_log(field.format(c=code) + ',"errorMessage":"rejected"')

    material = _material(ASSERT_500, ASSERT_TRACE, log("10001"))

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("10001"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("10002")))
    assert material is not None and "<codes:" in material and "=10001" in material


def test_brackets_in_a_log_line_and_quotes_in_the_message_keep_the_number() -> None:
    def log(code: str) -> str:
        return f"2026-10-16 10:00:01 [ERROR] gateway rejected payment [{code}]\n"

    def message(code: str) -> str:
        return (f'Payment status expected "DONE" but was "REJECTED" for gateway reply "{code}" '
                "after three attempts with the backup route")

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("10001"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("10002")))
    assert (_signature(message("10001"), ASSERT_TRACE, "")
            != _signature(message("10002"), ASSERT_TRACE, ""))


def test_compound_error_codes_keep_signatures_apart() -> None:
    def log(code: str) -> str:
        return ("2026-10-16 10:00:01 [INFO] c.e.Client - Response "
                f'{{"errorCode":"{code}","errorMessage":"rejected by gateway"}}\n')

    assert (_signature(ASSERT_500, ASSERT_TRACE, log("10001.1"))
            != _signature(ASSERT_500, ASSERT_TRACE, log("10002.1")))


def test_code_only_line_of_a_multiline_info_response_names_the_cause() -> None:
    def log(code: int) -> str:
        return f'2026-10-16 10:00:01 [INFO] c.e.Client - Response:\n{{\n  "errorCode": {code}\n}}\n'

    six = _signature(ASSERT_500, ASSERT_TRACE, log(6))

    assert six != _signature(ASSERT_500, ASSERT_TRACE, log(12))
    assert six != _signature(ASSERT_500, ASSERT_TRACE, "")


def test_journal_info_record_with_error_fields_is_an_error() -> None:
    def journal(message: str) -> str:
        return _journal({"logLevel": "INFO", "message": "POST /transfers"},
                        {"logLevel": "INFO", "message": "response", "errorCode": 6,
                         "errorMessage": message})

    material = _material(ASSERT_500, ASSERT_TRACE, journal("account is closed"))

    assert material is not None and "account is closed" in material
    assert "post /transfers" not in material


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
                "message": message, "details": {"attempt": index}}

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


class _Attachment:
    def __init__(self, name: str, content_type: str, content: bytes) -> None:
        self.meta = AttachmentMeta(id=100, name=name, type=content_type)
        self.content = content

    async def get_attachments_for_test_result(self, _test_id: int) -> list[AttachmentMeta]:
        return [self.meta]

    async def get_attachment_content(self, _attachment_id: int) -> bytes:
        return self.content


@pytest.mark.parametrize("content_type", ["application/x-ndjson", "text/plain"])
def test_json_line_log_names_the_cause_in_the_signature(content_type: str) -> None:
    def record(time: str, cause: str) -> str:
        return json.dumps({"@timestamp": time, "level": "ERROR", "message": "Charge failed",
                           "stack_trace": f"{cause}\n\tat c.e.Pay.charge(Pay.java:10)\n"})

    def signature(time: str, cause: str) -> tuple[str | None, str | None]:
        log = json.dumps({"@timestamp": time, "level": "INFO", "message": "start"})
        content = f"{log}\n{record(time, cause)}\n".encode()
        summary = FailedTestSummary(test_result_id=1, name="t", status="failed",
                                    status_message=ASSERT_500, status_trace=ASSERT_TRACE)
        service = LogExtractionService(_Attachment("pay.log", content_type, content),
                                       LogExtractionConfig())
        asyncio.run(service.enrich_with_logs([summary]))
        cluster, tests = make_single_test_cluster(ASSERT_500, ASSERT_TRACE)
        tests[1] = summary
        return cluster_signature(cluster, tests), signature_material(cluster, tests)

    pool, material = signature("2026-10-03T10:00:01Z", "java.sql.SQLException: pool exhausted")
    assert material is not None and "java.sql.sqlexception: pool exhausted" in material
    assert pool == signature("2026-10-05T23:59:59Z", "java.sql.SQLException: pool exhausted")[0]
    assert pool != signature("2026-10-03T10:00:01Z", "java.io.IOException: disk full")[0]


def test_multiline_json_message_names_the_cause_in_the_signature() -> None:
    def signature(message: str) -> str | None:
        record = json.dumps({"level": "ERROR", "message": message,
                             "stack_trace": "java.lang.IllegalStateException: rejected\n"
                                            "\tat c.e.Stock.reserve(Stock.java:10)\n"})
        return _signature(ASSERT_500, ASSERT_TRACE, make_error_log(record + "\n"))

    assert (signature("warehouse closed\nretry later")
            != signature("credentials revoked\nretry later"))


NAMES_THE_CAUSE = [
    (ASSERT_500, CAUSED_TRACE),
    ("missing order", "java.lang.IllegalStateException: missing order\n\tat a.B.c(B.java:1)"),
    ("customer is null",
     "java.lang.NullPointerException: customer is null\n\tat ru.company.OrderTest.create(OrderTest.java:7)"),
    ("Connection refused: billing-service:8443",
     "java.net.ConnectException: Connection refused: billing-service:8443\n"
     "\tat java.base/sun.nio.ch.Net.connect0(Native Method)"),
    # Ожидание в UI — не симптом: его различает локатор.
    ("Expected condition failed: waiting for visibility of element located by By.id: pay",
     "org.openqa.selenium.TimeoutException: Expected condition failed: waiting for visibility "
     "of element located by By.id: pay\n\tat ru.company.ui.PayPage.open(PayPage.java:12)"),
    # Корень решает: таймаут, обёрнутый причиной, которая называет себя.
    ("Export job 9 failed",
     "com.example.export.ExportException: Export job 9 failed\n\tat a.B.c(B.java:1)\n"
     "Caused by: java.net.SocketTimeoutException: Read timed out\n\tat a.B.d(B.java:2)\n"
     "Caused by: java.io.IOException: No space left on device\n\t... 3 more"),
]
SYMPTOMS = [
    (TIMEOUT, TIMEOUT_TRACE),
    ("I/O error on GET request for \"http://reports/api\": Read timed out",
     "org.springframework.web.client.ResourceAccessException: I/O error on GET request for "
     "\"http://reports/api\": Read timed out\n\tat a.B.c(B.java:1)\n"
     "Caused by: java.net.SocketTimeoutException: Read timed out\n\t... 12 more"),
    ("java.net.SocketException: Connection reset",
     "java.net.SocketException: Connection reset\n\tat java.base/sun.nio.ch.NioSocketImpl.implRead"
     "(NioSocketImpl.java:323)"),
    ("", "java.net.http.HttpTimeoutException: request timed out\n\tat a.B.c(B.java:1)"),
    ("", 'Traceback (most recent call last):\n  File "t.py", line 3, in test\n'
         "    resp = client.get('/reports')\nrequests.exceptions.ReadTimeout: "
         "HTTPConnectionPool(host='reports', port=80): Read timed out. (read timeout=5)"),
    ("", "    def test_report(client):\n>       client.get('/reports')\n"
         "E       socket.timeout: timed out\n\ntests/test_report.py:4: timeout"),
    ("", 'Traceback (most recent call last):\n  File "t.py", line 3, in test\n'
         "    sock.recv(1024)\nConnectionResetError: [Errno 104] Connection reset by peer"),
    # Ошибка статуса HTTP-клиента — как «expected 200 but was 500» (same_symptom_log_causes).
    ("httpx.HTTPStatusError: Server error '500 Internal Server Error' for url "
     "'http://policy-api:8080/v1/policies'",
     "    def test_issue(client):\n>       resp.raise_for_status()\n"
     "E       httpx.HTTPStatusError: Server error '500 Internal Server Error' for url "
     "'http://policy-api:8080/v1/policies'\n\ntests/test_policy.py:7: HTTPStatusError"),
    ("", 'Traceback (most recent call last):\n  File "t.py", line 3, in test\n'
         "    resp.raise_for_status()\nrequests.exceptions.HTTPError: 503 Server Error: "
         "Service Unavailable for url: http://reports/api"),
]


@pytest.mark.parametrize(("message", "trace"), NAMES_THE_CAUSE)
def test_error_that_names_the_cause_keeps_the_log_out(message: str, trace: str) -> None:
    pool = _signature(message, trace, PAYMENT_POOL_LOG)

    assert pool == _signature(message, trace, CATALOG_NPE_LOG) == _signature(message, trace, "")
    material = _material(message, trace, PAYMENT_POOL_LOG)
    assert material is not None and "+log\n" not in material
    assert not log_names_cause(message, trace)


@pytest.mark.parametrize(("message", "trace"), SYMPTOMS)
def test_symptom_exception_takes_the_log_errors(message: str, trace: str) -> None:
    """Голый таймаут или обрыв причину не называет: у nginx и у медленного запроса один и
    тот же ``SocketTimeoutException`` (эталон ``timeouts_two_causes``)."""
    assert is_symptom_exception(trace) and log_names_cause(message, trace)
    pool = _signature(message, trace, PAYMENT_POOL_LOG)

    assert pool != _signature(message, trace, CATALOG_NPE_LOG)
    assert _signature(message, trace, INFO_LOG) == _signature(message, trace, "")
    material = _material(message, trace, PAYMENT_POOL_LOG)
    assert material is not None and material.splitlines()[0].endswith("+log")


# Ветка Suppressed (try-with-resources): JVM печатает её с отступом таба, и её Caused by —
# тоже; Caused by основной цепочки — без отступа.
SUPPRESSED = ("\tSuppressed: java.lang.IllegalStateException: stream close failed\n"
              "\t\tat ru.company.reports.ReportClient.close(ReportClient.java:44)\n"
              "\tCaused by: java.io.IOException: Broken pipe\n"
              "\t\t... 4 more\n")


@pytest.mark.parametrize(("trace", "root_is_symptom"), [
    (TIMEOUT_TRACE + SUPPRESSED, True),
    # Основная цепочка после ветки: корень — её Caused by, а не причина подавленного.
    ("com.example.export.ExportException: Export job 9 failed\n\tat a.B.c(B.java:1)\n"
     + SUPPRESSED + "Caused by: java.net.SocketTimeoutException: Read timed out\n"
     "\t... 3 more\n", True),
    ("com.example.export.ExportException: Export job 9 failed\n\tat a.B.c(B.java:1)\n"
     "Caused by: java.net.SocketTimeoutException: Read timed out\n\tat a.B.d(B.java:2)\n"
     + SUPPRESSED, True),
    (TIMEOUT_TRACE + SUPPRESSED + "Caused by: java.io.IOException: No space left on device\n",
     False),
])
def test_suppressed_branch_is_not_the_root(trace: str, root_is_symptom: bool) -> None:
    assert is_symptom_exception(trace) is root_is_symptom
    pool = _signature(TIMEOUT, trace, PAYMENT_POOL_LOG)
    assert (pool != _signature(TIMEOUT, trace, CATALOG_NPE_LOG)) is root_is_symptom


def _suppressed(error: str, cause: str) -> str:
    return (f"\tSuppressed: {error}\n\t\tat ru.company.reports.ReportClient.close(ReportClient.java:44)\n"
            f"\tCaused by: {cause}\n\t\t... 4 more\n")


def test_suppressed_branches_stay_out_of_the_trace_anchor() -> None:
    """Ошибка закрытия ресурса — не причина: при той же основной цепочке сигнатура одна, а
    строки подавленных причин не вытесняют основную причину из якоря (до 4 строк)."""
    head = ("com.example.export.ExportException: Export job 9 failed\n\tat a.B.c(B.java:1)\n")
    main_cause = "Caused by: java.lang.IllegalStateException: export disk is read-only\n\t... 3 more\n"
    closing = "".join(_suppressed(f"java.lang.IllegalStateException: close {name} failed",
                                  f"java.io.IOException: {name} broken")
                      for name in ("a-stream", "a-socket"))
    other = _suppressed("java.lang.IllegalStateException: close channel failed",
                        "java.io.IOException: Connection reset by peer")
    message = "Export job 9 failed"

    plain = _signature(message, head + main_cause, PAYMENT_POOL_LOG)
    assert _signature(message, head + closing + main_cause, PAYMENT_POOL_LOG) == plain
    assert _signature(message, head + other + main_cause, PAYMENT_POOL_LOG) == plain
    material = _material(message, head + closing + main_cause, "")
    assert material is not None
    assert "export disk is read-only" in material and "broken" not in material


def test_suppressed_cause_does_not_cancel_a_generic_assertion() -> None:
    trace = ASSERT_TRACE.rstrip("\n") + "\n" + SUPPRESSED

    assert is_generic_assertion(ASSERT_500, trace)
    assert _signature(ASSERT_500, trace, PAYMENT_POOL_LOG) != _signature(
        ASSERT_500, trace, CATALOG_NPE_LOG)


def test_shared_background_does_not_make_different_errors_one_signature() -> None:
    background = ("2026-10-03 10:00:00 [ERROR] HealthCheck: redis ping failed\n"
                  "redis.clients.jedis.exceptions.JedisConnectionException: Read timed out\n")
    for message, trace in ((TIMEOUT, TIMEOUT_TRACE), (ASSERT_500, ASSERT_TRACE)):
        assert (_signature(message, trace, background + PAYMENT_POOL_LOG)
                != _signature(message, trace, background + CATALOG_NPE_LOG))


LONG_CONNECT = ("I/O error on POST request for the orders service: Failed to connect to "
                "localhost/127.0.0.1:{port} after three retries")
SELENIDE = "Element not found {{{locator}}}\nExpected: visible\nTimeout: 4 s."
RESOURCE_PAIRS = [
    (LONG_CONNECT.format(port=8080), LONG_CONNECT.format(port=5432)),
    (SELENIDE.format(locator="#Login"), SELENIDE.format(locator="#login")),
    ("Connection refused: auth-service:8080", "Connection refused: billing-service:8443"),
    ("Connection refused: 10.0.0.5:8080", "Connection refused: 10.0.0.6:8080"),
    ("TimeoutError: locator.click: Timeout 30000ms exceeded.\nCall log:\n"
     "  - waiting for locator('#promo-banner')",
     "TimeoutError: locator.click: Timeout 30000ms exceeded.\nCall log:\n"
     "  - waiting for locator('#gift-wrap-toggle')"),
]


@pytest.mark.parametrize(("first", "second"), RESOURCE_PAIRS)
def test_resources_that_split_clusters_split_signatures(first: str, second: str) -> None:
    """Что разделяет gate по ресурсам, разделяет и сигнатура: порт длинного сообщения
    нормализация сводит к ``<NUM>``, регистр локатора — к нижнему. Разный IP gate не делит."""
    def signature(message: str) -> str | None:
        trace = f"java.lang.RuntimeException: {message.splitlines()[0]}\n\tat a.B.c(B.java:1)"
        return _signature(message, trace, "")

    differ = message_resources(first).differ(message_resources(second))
    assert differ == (signature(first) != signature(second))


def test_resources_are_a_part_of_the_material() -> None:
    material = _material(SELENIDE.format(locator="#Login"), "", "")

    assert material is not None and material.splitlines()[0] == "message+resources"
    assert material.splitlines()[-1] == "locator:#Login"


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
