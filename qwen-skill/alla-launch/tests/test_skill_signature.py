"""Сигнатура проблемы: что входит в материал и когда в него попадает лог."""

from __future__ import annotations

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

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
