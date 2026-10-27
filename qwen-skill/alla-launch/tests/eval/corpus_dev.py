"""Синтетический корпус dev: по нему подбираются пороги, веса и правила.

Прогон на сценарий. Тексты — по публичным форматам логов (Spring Boot, logback, log4j2,
JUL, Python logging, logfmt, nginx, Celery, syslog; Go: klog, panic, logrus, zap, zerolog,
slog; JSON-логи Java: logstash-logback-encoder, ECS, JsonTemplateLayout) и по тому, как TestOps отвечает в
``skill_fake_testops.py``. Бывший holdout v2 (launch id 71xx) добавляет тесты Go testify/Gomega,
Jest, WebdriverIO, pytest, NUnit, xUnit, RSpec/Capybara, PHPUnit, Spock и их логи. Бывший
holdout v3 (launch id 81xx, сценарии ``*_v3``) — Kotest, ScalaTest, TestNG + Feign, Karate,
Newman, Cypress, TestCafe, Nightwatch, Espresso, MSTest, Minitest, ExUnit, Robot Framework,
Behave, Python unittest, Go testing, cargo test, Vitest, Mocha; логи Apache httpd, MongoDB
JSON, Redis, Cassandra, ClickHouse, Airflow, PHP, IBM MQ, Quarkus, WildFly, WebLogic, log4net,
Serilog CLEF, Sidekiq, env_logger, uvicorn, Akka, kubectl events, HAProxy, RabbitMQ, консоль
Chrome; общие логи сервисов с rqUID/operUID, correlationId и traceparent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from typing import Any

from eval.corpus import Case, LaunchBuilder, java_trace, noise_lines

ASSERT_500 = "expected: <200> but was: <500>"


def _assert_trace(message: str, test_class: str, method: str, line: int) -> str:
    return java_trace(f"java.lang.AssertionError: {message}", [
        "org.junit.Assert.fail(Assert.java:89)",
        "org.junit.Assert.assertEquals(Assert.java:120)",
        f"{test_class}.{method}({test_class.rsplit('.', 1)[-1]}.java:{line})",
        "java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)",
    ])


# ---------------------------------------------------------------------------
# Форматы логов
# ---------------------------------------------------------------------------

SPRING_BOOT_LOG = (
    "2026-10-03T10:00:00.101+03:00  INFO 12345 --- [payment] [nio-8080-exec-1] "
    "c.e.payment.PaymentController : POST /payments received\n"
    "2026-10-03T10:00:00.123+03:00 ERROR 12345 --- [payment] [nio-8080-exec-1] "
    "c.e.payment.PaymentService : Payment authorization failed for order 5512\n"
    "com.example.payment.GatewayException: Card processor rejected request: merchant terminal "
    "T-77 is blocked\n"
    "\tat com.example.payment.GatewayClient.authorize(GatewayClient.java:88)\n"
    "\tat com.example.payment.PaymentService.pay(PaymentService.java:41)\n"
    "2026-10-03T10:00:00.130+03:00  INFO 12345 --- [payment] [nio-8080-exec-1] "
    "c.e.payment.PaymentController : POST /payments completed status=500\n"
)
LOGBACK_LOG = (
    "10:00:00.100 [main] INFO  c.e.catalog.CatalogService - Loading catalog page 3\n"
    "10:00:00.123 [main] ERROR c.e.catalog.CatalogService - Failed to load catalog page 3\n"
    "java.lang.IllegalStateException: Price list PL-77 is not published\n"
    "\tat com.example.catalog.PriceRepository.current(PriceRepository.java:52)\n"
    "\tat com.example.catalog.CatalogService.page(CatalogService.java:30)\n"
    "10:00:00.140 [main] INFO  c.e.catalog.CatalogService - Request finished\n"
)
LOG4J2_LOG = (
    "2026-10-03 10:00:00,100 INFO  [main] c.e.profile.ProfileService: Updating profile 42\n"
    "2026-10-03 10:00:00,123 ERROR [main] c.e.profile.ProfileService: Profile update failed\n"
    "org.springframework.dao.DataIntegrityViolationException: could not execute statement\n"
    "\tat org.springframework.orm.jpa.vendor.HibernateJpaDialect.convert(HibernateJpaDialect.java:276)\n"
    "\tat com.example.profile.ProfileService.update(ProfileService.java:64)\n"
    "Caused by: org.postgresql.util.PSQLException: ERROR: duplicate key value violates unique "
    "constraint \"uk_profile_email\"\n"
    "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(QueryExecutorImpl.java:2713)\n"
    "\t... 42 more\n"
    "2026-10-03 10:00:00,150 INFO  [main] c.e.profile.ProfileController: PUT /profile -> 500\n"
)
JUL_LOG = (
    "Oct 03, 2026 10:00:00 AM com.example.notify.MailSender send\n"
    "INFO: Sending mail to user 42\n"
    "Oct 03, 2026 10:00:01 AM com.example.notify.MailSender send\n"
    "SEVERE: SMTP server rejected message: 554 5.7.1 Relay access denied\n"
    "javax.mail.MessagingException: 554 5.7.1 Relay access denied\n"
    "\tat com.sun.mail.smtp.SMTPTransport.issueSendCommand(SMTPTransport.java:2374)\n"
    "\tat com.example.notify.MailSender.send(MailSender.java:58)\n"
    "Oct 03, 2026 10:00:02 AM com.example.notify.MailSender send\n"
    "INFO: Mail queue drained\n"
)
PYTHON_ROOT_LOG = (
    "INFO:root:Building monthly report for account 42\n"
    "ERROR:root:Report generation failed: template 'monthly.xlsx' not found in /srv/templates\n"
    "INFO:root:Request finished\n"
)
PYTHON_FORMAT_LOG = (
    "2026-10-03 10:00:00,100 - search - INFO - Searching products for 'chair'\n"
    "2026-10-03 10:00:00,123 - search - ERROR - Elasticsearch query failed: "
    "index_not_found_exception [products_v2]\n"
    "2026-10-03 10:00:00,140 - search - INFO - Returned 0 products\n"
)
PYTHON_TRACEBACK_LOG = (
    "2026-10-03 10:00:00,100 - billing - INFO - Calculating invoice 77\n"
    "Traceback (most recent call last):\n"
    "  File \"/app/billing/invoice.py\", line 41, in calculate\n"
    "    total = sum(line.amount for line in lines)\n"
    "  File \"/app/billing/invoice.py\", line 41, in <genexpr>\n"
    "    total = sum(line.amount for line in lines)\n"
    "TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'\n"
    "2026-10-03 10:00:00,130 - billing - INFO - Request finished\n"
)
LOGFMT_LOG = (
    'time=2026-10-03T10:00:00.100Z level=info msg="reserve request" sku=SKU-1 qty=2\n'
    'time=2026-10-03T10:00:00.123Z level=error msg="failed to reserve stock" sku=SKU-1 '
    'err="stock service returned 409: reservation already exists"\n'
    'time=2026-10-03T10:00:00.140Z level=info msg="request finished" status=500\n'
)
NGINX_LOG = (
    '2026/10/03 10:00:00 [notice] 1#1: start worker process 123\n'
    '2026/10/03 10:00:01 [error] 123#0: *1 connect() failed (111: Connection refused) while '
    'connecting to upstream, client: 10.0.0.5, server: api, request: "GET /api/delivery/slots '
    'HTTP/1.1", upstream: "http://10.0.0.9:8080/api/delivery/slots"\n'
    '2026/10/03 10:00:02 [notice] 123#0: *2 client closed connection\n'
)
CURRENT_FORMAT_LOG = (
    "2026-10-03 10:00:00 [INFO] OrderService: request received\n"
    "2026-10-03 10:00:01 [ERROR] OrderService: failed to create order\n"
    "java.lang.NullPointerException: customer is null\n"
    "\tat ru.company.OrderService.create(OrderService.java:10)\n"
    "2026-10-03 10:00:02 [INFO] OrderService: request finished\n"
)
BARE_EXCEPTION_LOG = (
    "2026-10-03 10:00:00 [INFO] Scheduler: job export started\n"
    "com.example.export.ExportException: Export job 9 failed\n"
    "\tat com.example.export.Exporter.run(Exporter.java:77)\n"
    "Caused by: java.io.IOException: No space left on device\n"
    "\tat java.base/java.io.FileOutputStream.writeBytes(Native Method)\n"
    "\t... 12 more\n"
    "2026-10-03 10:00:01 [INFO] Scheduler: job export finished\n"
)

# (группа, причина, категория, сервис, сообщение теста, лог, имя вложения, доказательства)
_FORMATS: list[tuple[str, str, str, str, str, str, str, list[str]]] = [
    ("fmt-spring-boot", "payment-terminal-blocked", "окружение", "payment",
     "Payment status expected AUTHORIZED but was DECLINED", SPRING_BOOT_LOG, "payment.log",
     ["Card processor rejected request: merchant terminal T-77 is blocked"]),
    ("fmt-logback", "price-list-unpublished", "данные", "catalog",
     "Catalog page 3 expected 20 items but was 0", LOGBACK_LOG, "catalog.log",
     ["Price list PL-77 is not published"]),
    ("fmt-log4j2", "profile-email-duplicate", "данные", "profile",
     "Profile update expected HTTP 200 but got 500 Internal Server Error", LOG4J2_LOG,
     "profile.log",
     ['duplicate key value violates unique constraint "uk_profile_email"']),
    ("fmt-jul", "smtp-relay-denied", "окружение", "notify",
     "Mail for user 42 was not delivered within 30 seconds", JUL_LOG, "notify.log",
     ["SMTP server rejected message: 554 5.7.1 Relay access denied"]),
    ("fmt-python-root", "report-template-missing", "окружение", "reports",
     "Monthly report file was not created", PYTHON_ROOT_LOG, "reports.log",
     ["Report generation failed: template 'monthly.xlsx' not found"]),
    ("fmt-python-format", "search-index-missing", "окружение", "search",
     "Search for 'chair' returned 0 products, expected at least 1", PYTHON_FORMAT_LOG,
     "search.log",
     ["index_not_found_exception [products_v2]"]),
    ("fmt-python-traceback", "invoice-null-amount", "приложение", "billing",
     "Invoice 77 total was not calculated", PYTHON_TRACEBACK_LOG, "billing.log",
     ["TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'"]),
    ("fmt-logfmt", "stock-reservation-conflict", "данные", "stock",
     "Stock reservation for SKU-1 expected RESERVED but was ERROR", LOGFMT_LOG, "stock.log",
     ["stock service returned 409: reservation already exists"]),
    ("fmt-nginx", "delivery-upstream-down", "окружение", "delivery",
     "Delivery slots request failed with HTTP 502 Bad Gateway", NGINX_LOG, "nginx-error.log",
     ["connect() failed (111: Connection refused) while connecting to upstream"]),
    ("fmt-current", "order-customer-null", "приложение", "orders",
     "Order was not created: expected status CREATED", CURRENT_FORMAT_LOG, "app.log",
     ["java.lang.NullPointerException: customer is null"]),
    ("fmt-bare-exception", "export-disk-full", "окружение", "export",
     "Export file report-9.csv was not found", BARE_EXCEPTION_LOG, "scheduler.log",
     ["java.io.IOException: No space left on device"]),
]


def log_formats() -> Case:
    """Группа на формат лога: ошибка в логе — единственное указание на причину."""
    builder = LaunchBuilder(5101, "Log formats")
    for group, cause, category, service, message, log, log_name, evidence in _FORMATS:
        test_class = f"ru.company.{service}.{service.title()}Test"
        for index, method in enumerate(("check", "checkAgain")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 20 + index * 10),
                step=f"Проверить сервис {service}", log=log, log_name=log_name,
                evidence=evidence,
            )
    return builder.build("log_formats")


# ---------------------------------------------------------------------------
# Ловушки извлечения
# ---------------------------------------------------------------------------

TRAP_LOG4J2_LOG = (
    "2026-10-03 10:00:00,100 INFO  [main] c.e.web.ErrorController: ErrorController registered\n"
    "2026-10-03 10:00:00,110 INFO  [main] c.e.imp.ImportJob: Batch import finished: "
    "Processed 0 errors\n"
    "2026-10-03 10:00:00,123 ERROR [main] c.e.imp.ImportJob: Import of file prices.csv failed: "
    "column 'price' is empty in row 17\n"
    "2026-10-03 10:00:00,130 INFO  [main] c.e.imp.ImportJob: Import job stopped\n"
)
TRAP_INFO_AFTER_STACK_LOG = (
    "2026-10-03 10:00:00 [INFO] FraudService: checking order 5512\n"
    "2026-10-03 10:00:01 [ERROR] FraudService: Order 5512 rejected by fraud rule R-12\n"
    "com.example.fraud.FraudRejectedException: Order 5512 rejected by fraud rule R-12\n"
    "\tat com.example.fraud.FraudService.check(FraudService.java:33)\n"
    "2026-10-03 10:00:01 [INFO] HealthService: health check OK, 0 ERROR events in last minute\n"
)
TRAP_ERROR_WORD_LOG = (
    "2026-10-03 10:00:00 [INFO] Audit: user typed ERROR in search field\n"
    "2026-10-03 10:00:00 [INFO] Audit: ERROR page template cached\n"
    "2026-10-03 10:00:01 [ERROR] IndexService: Search index rebuild failed: disk quota "
    "exceeded on /var/lib/index\n"
    "2026-10-03 10:00:02 [INFO] IndexService: rebuild scheduled again in 5 minutes\n"
)


def log_traps() -> Case:
    """Строки со словом «error» не из ошибок рядом с настоящей ошибкой."""
    builder = LaunchBuilder(5102, "Log traps")
    traps = [
        ("trap-import", "price-csv-empty", "данные", "imports",
         "Imported prices count expected 120 but was 0", TRAP_LOG4J2_LOG,
         ["Import of file prices.csv failed: column 'price' is empty in row 17"]),
        ("trap-fraud", "fraud-rule-r12", "данные", "fraud",
         "Order 5512 status expected PAID but was REJECTED", TRAP_INFO_AFTER_STACK_LOG,
         ["Order 5512 rejected by fraud rule R-12"]),
        ("trap-index", "index-disk-quota", "окружение", "index",
         "Search results are stale: index version 41 expected 42", TRAP_ERROR_WORD_LOG,
         ["Search index rebuild failed: disk quota exceeded on /var/lib/index"]),
    ]
    for group, cause, category, service, message, log, evidence in traps:
        test_class = f"ru.company.{service}.{service.title()}Test"
        for index, method in enumerate(("verify", "verifyAgain")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 30 + index * 10),
                step=f"Проверить {service}", log=log, evidence=evidence,
            )
    return builder.build("log_traps")


# ---------------------------------------------------------------------------
# Сценарии падений
# ---------------------------------------------------------------------------

HIKARI_LOG = (
    "2026-10-03 10:00:00 [INFO] OrderController: POST /orders\n"
    "2026-10-03 10:00:30 [ERROR] OrderRepository: could not save order\n"
    "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, "
    "request timed out after 30000ms.\n"
    "\tat com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)\n"
    "\tat ru.company.orders.OrderRepository.save(OrderRepository.java:40)\n"
    "2026-10-03 10:00:30 [INFO] OrderController: POST /orders -> 500\n"
)
DISCOUNT_NPE_LOG = (
    "2026-10-03 10:00:00 [INFO] OrderController: POST /orders\n"
    "2026-10-03 10:00:00 [ERROR] DiscountService: failed to apply discount\n"
    "java.lang.NullPointerException: Cannot invoke \"Discount.percent()\" because "
    "\"discount\" is null\n"
    "\tat ru.company.orders.DiscountService.apply(DiscountService.java:27)\n"
    "2026-10-03 10:00:00 [INFO] OrderController: POST /orders -> 500\n"
)


CUSTOMER_NPE_LOG = (
    "2026-10-03 10:00:00 [INFO] OrderController: POST /orders\n"
    "2026-10-03 10:00:00 [ERROR] OrderService: failed to create order\n"
    "java.lang.NullPointerException: Cannot invoke \"Customer.id()\" because "
    "\"customer\" is null\n"
    "\tat ru.company.orders.OrderService.create(OrderService.java:52)\n"
    "2026-10-03 10:00:00 [INFO] OrderController: POST /orders -> 500\n"
)
DISCOUNT_EVIDENCE = "Cannot invoke \"Discount.percent()\" because \"discount\" is null"
DISCOUNT_METHODS = ("createDiscountOrder", "createPromoOrder", "createCouponOrder")
FIRST_METHODS = ("createOrder", "createBigOrder", "createGiftOrder")


def _orders_500(builder: LaunchBuilder,
                groups: list[tuple[str, str, str, str, tuple[str, ...]]]) -> None:
    """Падения ``OrderApiTest`` с одним assertion и шагом; различаются только логи."""
    test_class = "ru.company.orders.OrderApiTest"
    # Строки кадров у методов одного класса разные: проект стенда (E08) повторяет их.
    for group_index, (group, cause, log, evidence, methods) in enumerate(groups):
        for index, method in enumerate(methods):
            line = 40 + (group_index * len(methods) + index) * 7
            builder.add_failure(
                group, cause=cause, category="приложение", name=method,
                full_name=f"{test_class}.{method}", message=ASSERT_500,
                trace=_assert_trace(ASSERT_500, test_class, method, line),
                step="Отправить запрос POST /orders", log=log, evidence=[evidence],
            )


def same_assertion_db_vs_npe() -> Case:
    """Одинаковый assertion и шаг, разные серверные ошибки в логе — две проблемы."""
    builder = LaunchBuilder(5103, "Orders regression")
    _orders_500(builder, [
        ("orders-500-db", "orders-db-pool", HIKARI_LOG,
         "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
         FIRST_METHODS),
        ("orders-500-npe", "orders-discount-null", DISCOUNT_NPE_LOG, DISCOUNT_EVIDENCE,
         DISCOUNT_METHODS),
    ])
    return builder.build("same_assertion_db_vs_npe")


def same_assertion_two_npes() -> Case:
    """Одинаковый assertion и шаг, в логах два разных NPE — две проблемы.

    Кластеризация их склеивает намеренно: общий корневой класс исключения не даёт логу
    разделить пару (иначе шум лога дробил бы одну проблему). Различает их модель по
    примерам («СОГЛАСОВАННОСТЬ»); сценарий — данные стенда E08 и тестов примеров.
    """
    builder = LaunchBuilder(5113, "Orders regression")
    _orders_500(builder, [
        ("orders-500-customer", "orders-customer-null", CUSTOMER_NPE_LOG,
         "Cannot invoke \"Customer.id()\" because \"customer\" is null", FIRST_METHODS),
        ("orders-500-discount", "orders-discount-null", DISCOUNT_NPE_LOG, DISCOUNT_EVIDENCE,
         DISCOUNT_METHODS),
    ])
    return builder.build("same_assertion_two_npes")


def step_split_one_cause() -> Case:
    """Один NPE сервиса заказов, тесты падают на разных шагах — одна проблема.

    Кластеризация делит её намеренно: шаг — жёсткий gate, и log override его не обходит
    (иначе одинаковый фон лога склеивал бы разные шаги). Это цена осторожности, видна как
    близкая пара (``near_pairs``, развело ``step``); после разбора её закрывает пометка
    «одна причина» скилла, а не кластеризация.
    """
    builder = LaunchBuilder(5119, "Orders and checkout")
    evidence = "Cannot invoke \"Customer.id()\" because \"customer\" is null"
    # Строки кадров — как в исходниках стенда Qwen (E11): OrderApiTest — те же, что у E08.
    variants = [
        ("ru.company.orders.OrderApiTest", "Отправить запрос POST /orders", FIRST_METHODS, 40),
        ("ru.company.checkout.CheckoutTest", "Оформить заказ из корзины",
         ("checkoutCart", "checkoutWithBonus", "checkoutGuest"), 30),
    ]
    for test_class, step, methods, first_line in variants:
        for index, method in enumerate(methods):
            line = first_line + index * 7
            builder.add_failure(
                "orders-customer-null", cause="orders-customer-null", category="приложение",
                name=method, full_name=f"{test_class}.{method}", message=ASSERT_500,
                trace=_assert_trace(ASSERT_500, test_class, method, line),
                step=step, log=CUSTOMER_NPE_LOG, evidence=[evidence],
            )
    return builder.build("step_split_one_cause")


def _duplicate_notice(field: str, value: str, page_class: str, method: str,
                      test_class: str, test_method: str, line: int) -> tuple[str, str]:
    text = f"Ошибка: запись с таким значением уже существует ({field} = {value})"
    message = (f'Element should have text "Создано" {{.notification.error}}\n'
               f'Actual value: text="{text}"\nTimeout: 4 s.')
    trace = java_trace(f"com.codeborne.selenide.ex.ElementShould: {message}", [
        "com.codeborne.selenide.impl.WebElementSource.checkCondition(WebElementSource.java:79)",
        f"ru.company.ui.{page_class}.{method}({page_class}.java:22)",
        f"{test_class}.{test_method}({test_class.rsplit('.', 1)[-1]}.java:{line})",
    ])
    return message, trace


def unique_conflict_two_objects() -> Case:
    """Отказ проверки уникальности у ссылки и у папки — один класс, разные причины.

    Обратный к ``step_split_one_cause`` случай: текст ошибки почти тот же, тесты упали на
    разных шагах (близкая пара, развело ``step``), но ломаются разные объекты — адрес ссылки
    занят в данных стенда, папку оставил другой тест. Общий шаг «очистить данные» их не
    объединяет: шаг ``relate`` должен оставить их раздельными (стенд Qwen, E12).
    """
    builder = LaunchBuilder(5123, "Workspace UI")
    variants = [
        ("ws-link-alias", "link-alias-taken-in-stand-data", "link.alias", "promo-autumn",
         "LinkPage", "shouldBeCreated", "ru.company.links.LinksTest", "createShortLink", 27,
         "Проверить, что ссылка создана"),
        ("ws-folder-name", "folder-left-by-other-test", "folder.name", "Отчёты QA",
         "FolderPage", "create", "ru.company.folders.FoldersTest", "createFolder", 31,
         "Нажать кнопку «Создать папку»"),
    ]
    for group, cause, field, value, page_class, method, test_class, test_method, line, step \
            in variants:
        message, trace = _duplicate_notice(field, value, page_class, method,
                                           test_class, test_method, line)
        builder.add_failure(
            group, cause=cause, category="данные", name=test_method,
            full_name=f"{test_class}.{test_method}", message=message, trace=trace, step=step,
            evidence=[f"({field} = {value})"], status="broken",
        )
    return builder.build("unique_conflict_two_objects")


def auth_401_403() -> Case:
    """401 и 403 с похожим текстом — разные причины."""
    builder = LaunchBuilder(5104, "Access regression")
    test_class = "ru.company.access.AccessTest"
    groups = [
        ("access-401", "token-expired", "тест",
         'Expected status code <200> but was <401>. Response: {"error":"unauthorized",'
         '"message":"Token expired"}', ("listUsers", "listRoles", "listGroups")),
        ("access-403", "admin-role-missing", "данные",
         'Expected status code <200> but was <403>. Response: {"error":"forbidden",'
         '"message":"Role ADMIN required"}', ("deleteUser", "deleteRole", "deleteGroup")),
    ]
    for group, cause, category, message, methods in groups:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category=category, name=method,
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 15 + index * 6),
                step="Выполнить запрос администратора", evidence=[message.split(". ")[0]],
            )
    return builder.build("auth_401_403")


GATEWAY_TIMEOUT_LOG = (
    '2026/10/03 10:00:00 [error] 77#0: *9 upstream timed out (110: Connection timed out) while '
    'reading response header from upstream, client: 10.0.0.5, server: api, request: '
    '"GET /api/reports/sales HTTP/1.1", upstream: "http://10.0.0.12:8080/api/reports/sales"\n'
)
SLOW_QUERY_LOG = (
    "2026-10-03 10:00:00 [INFO] ReportService: building sales report\n"
    "2026-10-03 10:00:29 [ERROR] ReportRepository: query failed\n"
    "java.sql.SQLTimeoutException: Query execution was interrupted, maximum statement "
    "execution time exceeded\n"
    "\tat ru.company.reports.ReportRepository.sales(ReportRepository.java:88)\n"
)


def timeouts_two_causes() -> Case:
    """Одинаковый таймаут клиента, разные причины в логах."""
    builder = LaunchBuilder(5105, "Reports nightly")
    message = "java.net.SocketTimeoutException: Read timed out"
    trace = java_trace(message, [
        "java.base/sun.nio.ch.NioSocketImpl.timedRead(NioSocketImpl.java:288)",
        "ru.company.reports.ReportClient.get(ReportClient.java:31)",
    ])
    groups = [
        ("timeout-gateway", "reports-upstream-slow", "окружение", GATEWAY_TIMEOUT_LOG,
         "nginx-error.log", "upstream timed out (110: Connection timed out)",
         ("salesReport", "weeklyReport")),
        ("timeout-query", "reports-slow-query", "приложение", SLOW_QUERY_LOG, "app.log",
         "maximum statement execution time exceeded", ("stockReport", "yearReport")),
    ]
    for group, cause, category, log, log_name, evidence, methods in groups:
        for method in methods:
            builder.add_failure(
                group, cause=cause, category=category, name=method,
                full_name=f"ru.company.reports.ReportTest.{method}", message=message,
                trace=trace, step="Получить отчёт", log=log, log_name=log_name,
                evidence=[evidence], status="broken",
            )
    return builder.build("timeouts_two_causes")


def _selenide(selector: str, page_class: str, method: str) -> tuple[str, str]:
    message = f"Element not found {{{selector}}}\nExpected: visible\nTimeout: 4 s."
    trace = java_trace(f"com.codeborne.selenide.ex.ElementNotFound: {message}", [
        "com.codeborne.selenide.impl.WebElementSource.createElementNotFoundError"
        "(WebElementSource.java:31)",
        f"ru.company.ui.{page_class}.{method}({page_class}.java:22)",
    ])
    return message, trace


def selenide_pages() -> Case:
    """«Элемент не найден» на разных страницах — разные проблемы."""
    builder = LaunchBuilder(5106, "UI regression")
    pages = [
        ("ui-cart", "checkout-button-renamed", "тест", "#checkout-button", "CartPage",
         "Открыть корзину", ("checkoutGuest", "checkoutUser")),
        ("ui-profile", "avatar-cdn-down", "окружение", ".profile-avatar", "ProfilePage",
         "Открыть профиль", ("openProfile", "editProfile")),
    ]
    for group, cause, category, selector, page_class, step, methods in pages:
        for method in methods:
            message, trace = _selenide(selector, page_class, method)
            builder.add_failure(
                group, cause=cause, category=category, name=method,
                full_name=f"ru.company.ui.{page_class}Test.{method}", message=message,
                trace=trace, step=step, evidence=[f"Element not found {{{selector}}}"],
                status="broken",
            )
    return builder.build("selenide_pages")


def connect_services() -> Case:
    """``ConnectException`` к разным сервисам — разные проблемы окружения."""
    builder = LaunchBuilder(5107, "Integration nightly")
    services = [
        ("connect-auth", "auth-service-down", "auth-service:8080", "AuthClient",
         ("login", "logout", "refresh")),
        ("connect-billing", "billing-service-down", "billing-service:8443", "BillingClient",
         ("charge", "refund")),
    ]
    for group, cause, address, client, methods in services:
        message = f"java.net.ConnectException: Connection refused: {address}"
        trace = java_trace(message, [
            "java.base/sun.nio.ch.Net.connect0(Native Method)",
            f"ru.company.clients.{client}.call({client}.java:19)",
        ])
        for method in methods:
            builder.add_failure(
                group, cause=cause, category="окружение", name=method,
                full_name=f"ru.company.integration.{client}Test.{method}", message=message,
                trace=trace, step=f"Вызвать {client}", evidence=[f"Connection refused: {address}"],
                status="broken",
            )
    return builder.build("connect_services")


def data_validation() -> Case:
    """Ошибки валидации тестовых данных по разным полям."""
    builder = LaunchBuilder(5108, "Clients regression")
    fields = [
        ("validation-inn", "inn-generator-9-digits", "field 'inn' must contain 10 or 12 digits",
         ("createCompany", "updateCompany")),
        ("validation-birth", "birth-date-in-future", "field 'birthDate' must be in the past",
         ("createPerson", "updatePerson")),
    ]
    for group, cause, detail, methods in fields:
        message = (f"Expected status code <201> but was <400>. Response: "
                   f'{{"error":"validation","message":"Validation failed: {detail}"}}')
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category="данные", name=method,
                full_name=f"ru.company.clients.ClientTest.{method}", message=message,
                trace=_assert_trace(message, "ru.company.clients.ClientTest", method, 50 + index),
                step="Создать клиента", evidence=[f"Validation failed: {detail}"],
            )
    return builder.build("data_validation")


def no_data() -> Case:
    """Падения без сообщения, стека и лога: причина неизвестна у каждого своя."""
    builder = LaunchBuilder(5109, "Silent failures")
    for index, (name, step) in enumerate((
        ("exportCsv", None), ("importXml", None), ("syncLdap", "Синхронизировать LDAP"),
    )):
        builder.add_failure(
            f"silent-{index + 1}", cause=None, name=name,
            full_name=f"ru.company.misc.SilentTest.{name}", step=step,
        )
    builder.add_result(name="healthCheck", status="passed")
    return builder.build("no_data")


AUTH_DOWN_LOG = (
    "2026-10-03 10:00:00 [INFO] Gateway: GET /api/cart\n"
    "2026-10-03 10:00:05 [ERROR] Gateway: auth-service health check failed: "
    "503 Service Unavailable\n"
    "2026-10-03 10:00:05 [INFO] Gateway: GET /api/cart -> 502\n"
)


def auth_down_symptoms() -> Case:
    """Разные симптомы одной причины — допустимо остаются разными группами."""
    builder = LaunchBuilder(5110, "Shop smoke")
    timeout = "java.net.SocketTimeoutException: Read timed out"
    ui_message, ui_trace = _selenide("#login-form", "LoginPage", "open")
    cases: list[tuple[str, str, str, str | None, str | None, list[str], tuple[str, ...]]] = [
        ("auth-down-502", "expected: <200> but was: <502>", "Открыть корзину", AUTH_DOWN_LOG,
         None, ["auth-service health check failed: 503 Service Unavailable"],
         ("cartApi", "wishlistApi")),
        ("auth-down-timeout", timeout, "Войти по паролю", None,
         java_trace(timeout, ["ru.company.shop.AuthClient.login(AuthClient.java:12)"]),
         ["Read timed out"], ("loginApi", "loginSmsApi")),
        ("auth-down-ui", ui_message, "Открыть страницу входа", None, ui_trace,
         ["Element not found {#login-form}"], ("loginUi", "registerUi")),
    ]
    for group, message, step, log, trace, evidence, methods in cases:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause="auth-service-down", category="окружение", name=method,
                full_name=f"ru.company.shop.ShopTest.{method}", message=message,
                trace=trace or _assert_trace(message, "ru.company.shop.ShopTest", method,
                                             10 + index),
                step=step, log=log, evidence=evidence, status="broken",
            )
    return builder.build("auth_down_symptoms")


PAYMENT_POOL_LOG = (
    "2026-10-03 10:00:00 [INFO] PaymentController: POST /payments\n"
    "2026-10-03 10:00:30 [ERROR] PaymentRepository: could not load payment\n"
    "java.sql.SQLTransientConnectionException: HikariPool-2 - Connection is not available, "
    "request timed out after 30000ms.\n"
    "\tat com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)\n"
    "\tat ru.company.payments.PaymentRepository.find(PaymentRepository.java:57)\n"
    "2026-10-03 10:00:30 [INFO] PaymentController: POST /payments -> 500\n"
)
CATALOG_NPE_LOG = (
    "2026-10-03 10:00:00 [INFO] CatalogController: GET /catalog\n"
    "2026-10-03 10:00:00 [ERROR] PriceService: failed to build price list\n"
    "java.lang.NullPointerException: Cannot invoke \"Price.amount()\" because \"price\" is null\n"
    "\tat ru.company.catalog.PriceService.list(PriceService.java:33)\n"
    "2026-10-03 10:00:00 [INFO] CatalogController: GET /catalog -> 500\n"
)
POOL_EVIDENCE = "HikariPool-2 - Connection is not available, request timed out after 30000ms."


def known_issue_symptoms() -> Case:
    """Одна причина (пул БД платежей) — три симптома, у каждого строка пула в логе.

    Кластеры остаются разными (разные симптомы), но запись базы знаний с признаком пула
    должна предлагаться всем трём и не предлагаться посторонней проблеме (шаг 6, стенд E10).
    """
    builder = LaunchBuilder(5112, "Payments regression")
    test_class = "ru.company.payments.PaymentTest"
    timeout = "java.net.SocketTimeoutException: Read timed out"
    ui_message, ui_trace = _selenide("#payment-status", "PaymentPage", "status")
    cases: list[tuple[str, str, str, str, str | None, str | None, tuple[str, ...]]] = [
        ("pay-pool-500", "payment-db-pool", ASSERT_500, "Оплатить заказ", PAYMENT_POOL_LOG, None,
         ("payByCard", "payBySbp")),
        ("pay-pool-timeout", "payment-db-pool", timeout, "Вернуть платёж", PAYMENT_POOL_LOG,
         java_trace(timeout, ["ru.company.payments.PaymentClient.refund(PaymentClient.java:19)"]),
         ("refundFull", "refundPartial")),
        ("pay-pool-ui", "payment-db-pool", ui_message, "Открыть статус оплаты", PAYMENT_POOL_LOG,
         ui_trace, ("paymentStatusUi", "paymentStatusMobile")),
        ("catalog-npe", "catalog-price-null", ASSERT_500, "Открыть каталог", CATALOG_NPE_LOG, None,
         ("catalogPage", "catalogSearch")),
    ]
    # Строки кадров у методов одного класса разные: проект стенда (E10) повторяет их.
    for group_index, (group, cause, message, step, log, trace, methods) in enumerate(cases):
        evidence = POOL_EVIDENCE if log is PAYMENT_POOL_LOG else (
            "Cannot invoke \"Price.amount()\" because \"price\" is null")
        for index, method in enumerate(methods):
            line = 30 + (group_index * len(methods) + index) * 9
            builder.add_failure(
                group, cause=cause, category="приложение", name=method,
                full_name=f"{test_class}.{method}", message=message,
                trace=trace or _assert_trace(message, test_class, method, line),
                step=step, log=log, evidence=[evidence], status="broken" if trace else "failed",
            )
    return builder.build("known_issue_symptoms")


def retries() -> Case:
    """Ретраи для шага 5: hidden-попытки с полями связи (``historyId``, ``testCaseId``…).

    Группы — по финальным активным падениям; разметка ``retries`` — какие попытки к какому
    финальному результату относятся и та ли у них ошибка.
    """
    builder = LaunchBuilder(5111, "Retries")
    test_class = "ru.company.cart.CartTest"

    def extra(history: str, case_id: int, browser: str = "chrome",
              stand: str = "stage-1") -> dict[str, object]:
        return {
            "historyId": history, "testCaseId": case_id,
            "parameters": [{"name": "browser", "value": browser}],
            "environment": [{"name": "stand", "value": stand}],
        }

    flaky = "expected: <3> but was: <2>"
    stable = "Cart total expected 300.00 but was 0.00"
    # прошёл после повтора
    builder.add_result(name="addItem", status="failed", message=flaky, hidden=True,
                       extra=extra("h-add", 501))
    passed = builder.add_result(name="addItem", status="passed", extra=extra("h-add", 501))
    builder.expect_passed_after_retry(passed)
    # все попытки с одной ошибкой
    same = [builder.add_result(name="cartTotal", status="failed", message=stable, hidden=True,
                               extra=extra("h-total", 502)) for _ in range(2)]
    final = builder.add_failure(
        "retry-same", cause="cart-total-zero", category="приложение", name="cartTotal",
        full_name=f"{test_class}.cartTotal", message=stable,
        trace=_assert_trace(stable, test_class, "cartTotal", 33), step="Проверить сумму",
        evidence=[stable], extra=extra("h-total", 502),
    )
    builder.expect_attempts(final, [(attempt, True) for attempt in same])
    # попытки с разными ошибками
    other = builder.add_result(name="removeItem", status="broken",
                               message="java.net.ConnectException: Connection refused: cart:8080",
                               hidden=True, extra=extra("h-remove", 503))
    removed = "Item SKU-9 is still in cart after removal"
    final = builder.add_failure(
        "retry-different", cause="cart-remove-ignored", category="приложение",
        name="removeItem", full_name=f"{test_class}.removeItem", message=removed,
        trace=_assert_trace(removed, test_class, "removeItem", 48), step="Удалить товар",
        evidence=[removed], extra=extra("h-remove", 503),
    )
    builder.expect_attempts(final, [(other, False)])
    # параметризованный тест: один testCaseId, разные параметры
    coupon = "Coupon SPRING applied discount 0% instead of 10%"
    for browser in ("chrome", "firefox"):
        attempt = builder.add_result(name=f"applyCoupon[{browser}]", status="failed",
                                     message=coupon, hidden=True,
                                     extra=extra(f"h-coupon-{browser}", 504, browser))
        final = builder.add_failure(
            "retry-param", cause="coupon-spring-expired", category="данные",
            name=f"applyCoupon[{browser}]", full_name=f"{test_class}.applyCoupon",
            message=coupon, trace=_assert_trace(coupon, test_class, "applyCoupon", 61),
            step="Применить купон", evidence=[coupon],
            extra=extra(f"h-coupon-{browser}", 504, browser),
        )
        builder.expect_attempts(final, [(attempt, True)])
    # смена окружения между попытками: попытка другого окружения — не повтор
    checkout = "Checkout returned HTTP 503 Service Unavailable"
    builder.add_result(name="checkout", status="failed", message=checkout, hidden=True,
                       extra=extra("h-checkout-1", 505, stand="stage-1"))
    final = builder.add_failure(
        "retry-env", cause="checkout-stand-down", category="окружение", name="checkout",
        full_name=f"{test_class}.checkout", message=checkout,
        trace=_assert_trace(checkout, test_class, "checkout", 75), step="Оформить заказ",
        evidence=[checkout], extra=extra("h-checkout-2", 505, stand="stage-2"),
    )
    builder.expect_attempts(final, [])
    # ошибки попытки нет в списке результатов — только в GET /api/testresult/{id}
    attempt = builder.add_result(name="updateQty", status="broken", hidden=True,
                                 extra=extra("h-qty", 506))
    builder.add_detail(attempt, trace="java.net.SocketTimeoutException: Read timed out\n"
                                      "\tat ru.company.cart.CartClient.update(CartClient.java:40)")
    quantity = "Quantity of SKU-3 expected 2 but was 1"
    final = builder.add_failure(
        "retry-detail", cause="cart-qty-lost", category="приложение", name="updateQty",
        full_name=f"{test_class}.updateQty", message=quantity,
        trace=_assert_trace(quantity, test_class, "updateQty", 90), step="Изменить количество",
        evidence=[quantity], extra=extra("h-qty", 506),
    )
    builder.expect_attempts(final, [(attempt, False)])
    return builder.build("retries")


def retries_by_context() -> Case:
    """Повторы без ``historyId``/``historyKey``: ключ — ``testCaseId`` + параметры + окружение.

    У попытки ``renewOnStage2`` отличается только окружение — это не повтор финального
    результата (в ``retries`` при смене стенда меняется и ``historyId``, поэтому окружение
    там не проверяется).
    """
    builder = LaunchBuilder(5118, "Subscriptions")
    test_class = "ru.company.subscriptions.SubscriptionTest"

    def extra(case_id: int, plan: str = "monthly", stand: str = "stage-1") -> dict[str, object]:
        return {"testCaseId": case_id, "parameters": [{"name": "plan", "value": plan}],
                "environment": [{"name": "stand", "value": stand}]}

    def failure(group: str, cause: str, name: str, message: str, step: str,
                fields: dict[str, object]) -> int:
        method = name.split("[", 1)[0]
        return builder.add_failure(
            group, cause=cause, category="приложение", name=name,
            full_name=f"{test_class}.{method}", message=message,
            trace=_assert_trace(message, test_class, method, 40), step=step,
            evidence=[message], extra=fields,
        )

    # тот же контекст: попытка связана, ошибка та же
    renew = "Subscription renewal expected ACTIVE but was GRACE"
    attempt = builder.add_result(name="renewMonthly", status="failed", message=renew,
                                 hidden=True, extra=extra(701))
    final = failure("ctx-same", "renewal-grace-period", "renewMonthly", renew,
                    "Продлить подписку", extra(701))
    builder.expect_attempts(final, [(attempt, True)])
    # отличается только окружение: попытка другого стенда — не повтор
    invoice = "Invoice for subscription expected 1 line but was 2"
    builder.add_result(name="renewOnStage2", status="failed", message=invoice, hidden=True,
                       extra=extra(702, stand="stage-1"))
    final = failure("ctx-env", "invoice-duplicate-line", "renewOnStage2", invoice,
                    "Выставить счёт", extra(702, stand="stage-2"))
    builder.expect_attempts(final, [])
    # один testCaseId, разные параметры: у каждого финального — своя попытка
    downgrade = "Downgrade price expected 299.00 but was 0.00"
    for plan in ("monthly", "yearly"):
        attempt = builder.add_result(name=f"downgrade[{plan}]", status="failed",
                                     message=downgrade, hidden=True, extra=extra(703, plan))
        final = failure("ctx-param", "downgrade-price-zero", f"downgrade[{plan}]", downgrade,
                        "Понизить тариф", extra(703, plan))
        builder.expect_attempts(final, [(attempt, True)])
    return builder.build("retries_by_context")


# ---------------------------------------------------------------------------
# Шум в логах одной проблемы
# ---------------------------------------------------------------------------

HEALTH_ERRORS_LOG = (
    "2026-10-03 10:01:05 [ERROR] [scheduler-1] HealthIndicator: Redis health check failed: "
    "Unable to connect to redis-cache:6379\n"
    "io.lettuce.core.RedisConnectionException: Unable to connect to redis-cache:6379\n"
    "\tat io.lettuce.core.RedisClient.connect(RedisClient.java:216)\n"
    "2026-10-03 10:01:06 [ERROR] [scheduler-1] KafkaListener: consumer group rebalance failed "
    "for topic inventory.events\n"
)


def _reservation_log(thread: str, line: int, reservation: str, request: str) -> str:
    return (
        f"2026-10-03 10:01:00 [INFO] [{thread}] ReservationController: "
        f"POST /reservations/{reservation}/confirm request_id={request}\n"
        f"2026-10-03 10:01:01 [ERROR] [{thread}] ReservationService: confirm failed for "
        f"reservation {reservation} request_id={request}\n"
        f"java.lang.IllegalStateException: Reservation {reservation} expired before "
        "confirmation\n"
        f"\tat ru.company.inventory.ReservationService.confirm(ReservationService.java:{line})\n"
        "\tat ru.company.inventory.ReservationController.confirm"
        f"(ReservationController.java:{line + 31})\n"
    )


def _delivery_log(connection: int, client: int, path: str) -> str:
    return (
        f"2026/10/03 10:02:00 [error] 31#0: *{connection} upstream timed out (110: Connection "
        f"timed out) while reading response header from upstream, client: 10.1.0.{client}, "
        f'server: delivery, request: "POST {path} HTTP/1.1", upstream: '
        f'"http://10.1.0.40:9090{path}"\n'
    )


def log_noise_one_problem() -> Case:
    """Логи одной проблемы различаются потоками, строками, id и посторонними ошибками.

    Такие различия — не другая проблема: группа не должна дробиться. Тест без лога — часть
    группы.
    """
    builder = LaunchBuilder(5116, "Inventory and delivery")
    reservation = "Reservation confirmation expected CONFIRMED but was EXPIRED"
    test_class = "ru.company.inventory.ReservationTest"
    logs: list[str | None] = [
        _reservation_log("http-nio-8080-exec-1", 88, "RSV-5521", "a1b2c3d4"),
        _reservation_log("http-nio-8080-exec-7", 88, "RSV-90817", "ffe9017a"),
        _reservation_log("nio-8080-exec-3", 93, "RSV-12", "zz-9") + HEALTH_ERRORS_LOG,
        _reservation_log("pool-3-thread-2", 101, "RSV-77310", "c0ffee11"),
        None,
    ]
    methods = ("confirmOne", "confirmBulk", "confirmGift", "confirmPreorder", "confirmAgain")
    for index, (method, log) in enumerate(zip(methods, logs)):
        builder.add_failure(
            "inv-reservation-expired", cause="reservation-ttl-too-short", category="приложение",
            name=method, full_name=f"{test_class}.{method}", message=reservation,
            trace=_assert_trace(reservation, test_class, method, 20 + index * 9),
            step="Подтвердить резерв", log=log, log_name="inventory.log",
            evidence=["expired before confirmation"] if log else [],
        )
    quote = "Delivery quote request failed with HTTP 504 Gateway Timeout"
    test_class = "ru.company.delivery.QuoteTest"
    for index, (method, path) in enumerate((
        ("quoteCourier", "/api/quotes/courier"), ("quotePickup", "/api/quotes/pickup-points"),
        ("quotePost", "/api/quotes/post"),
    )):
        builder.add_failure(
            "delivery-gateway-timeout", cause="delivery-quotes-slow", category="окружение",
            name=method, full_name=f"{test_class}.{method}", message=quote,
            trace=_assert_trace(quote, test_class, method, 30 + index * 6),
            step="Рассчитать доставку", log=_delivery_log(index * 13 + 4, index * 7 + 5, path),
            log_name="nginx-error.log", evidence=["upstream timed out (110: Connection timed out)"],
        )
    return builder.build("log_noise_one_problem")


REDIS_HEALTH_LOG = (
    "2026-10-03 10:04:05 [ERROR] [scheduler-1] HealthIndicator: Redis health check failed\n"
    "io.lettuce.core.RedisConnectionException: Unable to connect to redis-sessions:6379\n"
    "\tat io.lettuce.core.RedisClient.connect(RedisClient.java:216)\n"
)
KAFKA_REBALANCE_LOG = (
    "2026-10-03 10:04:06 [ERROR] [kafka-listener-2] ShipmentListener: consumer poll failed "
    "for topic shipments.events\n"
    "org.apache.kafka.common.errors.RebalanceInProgressException: rebalance in progress\n"
    "\tat org.apache.kafka.clients.consumer.KafkaConsumer.poll(KafkaConsumer.java:1250)\n"
)
BASKET_POOL_LOG = (
    "2026-10-03 10:04:00 [ERROR] [http-nio-8080-exec-2] BasketRepository: could not save basket\n"
    "java.sql.SQLTransientConnectionException: HikariPool-3 - Connection is not available, "
    "request timed out after 30000ms.\n"
    "\tat ru.company.basket.BasketRepository.save(BasketRepository.java:61)\n"
)
BASKET_NPE_LOG = (
    "2026-10-03 10:04:00 [ERROR] [http-nio-8080-exec-4] BasketService: failed to merge baskets\n"
    "java.lang.NullPointerException: Cannot invoke \"Basket.items()\" because \"guest\" is null\n"
    "\tat ru.company.basket.BasketService.merge(BasketService.java:44)\n"
)
WISHLIST_LOG = (
    "2026-10-03 10:04:00 [ERROR] [http-nio-8080-exec-6] WishlistService: wishlist sync failed\n"
    "java.lang.IllegalArgumentException: Unknown wishlist visibility PRIVATE_LINK\n"
    "\tat ru.company.wishlist.WishlistService.sync(WishlistService.java:73)\n"
)


def background_log_errors() -> Case:
    """Фоновые ошибки (health check, Kafka) в логах рядом с настоящей ошибкой.

    Разный фон не дробит одну NPE — и когда та же NPE есть у падения с другим симптомом
    (``basket-npe-direct``). Одинаковый фон у пула БД и NPE их склеивает: по паре общую
    фоновую ошибку не отличить от общей ошибки падения, и gate выбирает не дробить
    (различает модель по примерам).
    """
    builder = LaunchBuilder(5117, "Basket regression")
    message = "Basket merge expected status 200 but was 500"
    test_class = "ru.company.basket.BasketApiTest"
    tests = [
        ("basket-500-db", "basket-db-pool", BASKET_POOL_LOG + REDIS_HEALTH_LOG,
         "HikariPool-3 - Connection is not available", "mergeGuestBasket"),
        ("basket-500-db", "basket-db-pool", BASKET_POOL_LOG + KAFKA_REBALANCE_LOG,
         "HikariPool-3 - Connection is not available", "mergeUserBasket"),
        ("basket-500-npe", "basket-guest-null", BASKET_NPE_LOG + REDIS_HEALTH_LOG,
         "because \"guest\" is null", "mergeEmptyBasket"),
        ("basket-500-npe", "basket-guest-null", BASKET_NPE_LOG + KAFKA_REBALANCE_LOG,
         "because \"guest\" is null", "mergeSharedBasket"),
        ("basket-500-npe", "basket-guest-null", BASKET_NPE_LOG, "because \"guest\" is null",
         "mergeAfterLogin"),
    ]
    for index, (group, cause, log, evidence, method) in enumerate(tests):
        builder.add_failure(
            group, cause=cause, category="приложение", name=method,
            full_name=f"{test_class}.{method}", message=message,
            trace=_assert_trace(message, test_class, method, 30 + index * 8),
            step="Объединить корзины", log=log, log_name="basket.log", evidence=[evidence],
        )
    # Та же NPE за другим симптомом: клиент бросил исключение вместо ответа 500.
    direct = ('org.springframework.web.client.HttpServerErrorException$InternalServerError: '
              '500 Internal Server Error: "guest basket merge failed"')
    builder.add_failure(
        "basket-npe-direct", cause="basket-guest-null", category="приложение",
        name="mergeViaClient", full_name=f"{test_class}.mergeViaClient", message=direct,
        trace=java_trace(direct, [
            "org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:915)",
            f"{test_class}.mergeViaClient(BasketApiTest.java:88)"]),
        step="Объединить корзины", log=BASKET_NPE_LOG + KAFKA_REBALANCE_LOG,
        log_name="basket.log", evidence=["because \"guest\" is null"], status="broken",
    )
    wishlist = "Wishlist sync expected 3 items but was 0"
    for index, method in enumerate(("syncWishlist", "syncSharedWishlist")):
        builder.add_failure(
            "wishlist-visibility", cause="wishlist-visibility-unknown", category="данные",
            name=method, full_name=f"ru.company.wishlist.WishlistTest.{method}",
            message=wishlist,
            trace=_assert_trace(wishlist, "ru.company.wishlist.WishlistTest", method,
                                12 + index * 5),
            step="Синхронизировать избранное",
            log=WISHLIST_LOG + REDIS_HEALTH_LOG + KAFKA_REBALANCE_LOG, log_name="wishlist.log",
            evidence=["Unknown wishlist visibility PRIVATE_LINK"],
        )
    return builder.build("background_log_errors")


CHECKOUT_POOL_LOG = (
    "2026-10-03 11:10:00 [ERROR] [http-nio-8080-exec-3] OrderRepository: could not reserve stock\n"
    "java.sql.SQLTransientConnectionException: HikariPool-7 - Connection is not available, "
    "request timed out after 30000ms.\n"
    "\tat ru.shop.checkout.OrderRepository.reserve(OrderRepository.java:88)\n"
)
CHECKOUT_PROMO_LOG = (
    "2026-10-03 11:10:00 [ERROR] [http-nio-8080-exec-5] PromoService: promo pricing failed\n"
    "java.lang.NullPointerException: Cannot invoke \"Promo.discount()\" because \"promo\" is null\n"
    "\tat ru.shop.checkout.PromoService.apply(PromoService.java:52)\n"
)


def same_exception_two_log_causes() -> Case:
    """Одно исключение клиента у двух причин из лога сервиса, общий фон в логах.

    Исключение клиента называет причину само, поэтому лог в его сигнатуру не входит: пул БД
    и NPE промокода за одним «500 … order submit failed» получают одну сигнатуру, а общая
    фоновая ошибка (Redis) держит пары в одном кластере. Запись базы знаний о пуле,
    подтверждённая для такого кластера, не должна разбирать его без модели: что в группе две
    проблемы, видно только по логам примеров. У пула вторая группа — общий ассерт.
    """
    builder = LaunchBuilder(6140, "Checkout nightly")
    test_class = "ru.shop.checkout.CheckoutApiTest"
    direct = ("org.springframework.web.client.HttpServerErrorException$InternalServerError: "
              '500 Internal Server Error: "order submit failed"')
    groups = [
        ("submit-pool", "checkout-db-pool", CHECKOUT_POOL_LOG + REDIS_HEALTH_LOG,
         "HikariPool-7 - Connection is not available", ("submitOrder", "submitGiftOrder")),
        ("submit-promo", "checkout-promo-null", CHECKOUT_PROMO_LOG + REDIS_HEALTH_LOG,
         "because \"promo\" is null", ("submitPromoOrder", "submitPromoGiftOrder")),
    ]
    for group, cause, log, evidence, methods in groups:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category="приложение", name=method,
                full_name=f"{test_class}.{method}", message=direct,
                trace=java_trace(direct, [
                    "org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:915)",
                    f"{test_class}.{method}(CheckoutApiTest.java:{40 + index * 9})"]),
                step="Оформить заказ", log=log, log_name="checkout.log", evidence=[evidence],
                status="broken",
            )
    status = "Checkout status expected 200 but was 500"
    for index, method in enumerate(("payOrder", "payGiftOrder")):
        builder.add_failure(
            "pay-pool", cause="checkout-db-pool", category="приложение", name=method,
            full_name=f"{test_class}.{method}", message=status,
            trace=_assert_trace(status, test_class, method, 90 + index * 7),
            step="Оплатить заказ", log=CHECKOUT_POOL_LOG, log_name="checkout.log",
            evidence=["HikariPool-7 - Connection is not available"],
        )
    return builder.build("same_exception_two_log_causes")


def same_exception_split_log_causes() -> Case:
    """Одно исключение клиента у двух причин, логи без общего фона: кластеры разные.

    Gate по логу разводит пул БД и NPE промокода, а сигнатура у них одна: исключение называет
    причину само, и лог в неё не входит (``signature_shared_wrong`` 1 — известная граница
    сигнатуры). Запись о пуле, подтверждённая для его кластера, узнаёт кластер NPE «точно» —
    разбирать его по записи без модели нельзя: лог не тот, что видел инженер (ревью Codex).
    """
    builder = LaunchBuilder(6141, "Checkout nightly")
    test_class = "ru.shop.checkout.CheckoutApiTest"
    direct = ("org.springframework.web.client.HttpServerErrorException$InternalServerError: "
              '500 Internal Server Error: "order submit failed"')
    groups = [
        ("submit-pool", "checkout-db-pool", CHECKOUT_POOL_LOG,
         "HikariPool-7 - Connection is not available", ("submitOrder", "submitGiftOrder")),
        ("submit-promo", "checkout-promo-null", CHECKOUT_PROMO_LOG,
         "because \"promo\" is null", ("submitPromoOrder", "submitPromoGiftOrder")),
    ]
    for group, cause, log, evidence, methods in groups:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category="приложение", name=method,
                full_name=f"{test_class}.{method}", message=direct,
                trace=java_trace(direct, [
                    "org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:915)",
                    f"{test_class}.{method}(CheckoutApiTest.java:{40 + index * 9})"]),
                step="Оформить заказ", log=log, log_name="checkout.log", evidence=[evidence],
                status="broken",
            )
    status = "Checkout status expected 200 but was 500"
    for index, method in enumerate(("payOrder", "payGiftOrder")):
        builder.add_failure(
            "pay-pool", cause="checkout-db-pool", category="приложение", name=method,
            full_name=f"{test_class}.{method}", message=status,
            trace=_assert_trace(status, test_class, method, 90 + index * 7),
            step="Оплатить заказ", log=CHECKOUT_POOL_LOG, log_name="checkout.log",
            evidence=["HikariPool-7 - Connection is not available"],
        )
    return builder.build("same_exception_split_log_causes")


def _payment_response(code: str, text: str, request: str) -> tuple[str, str, str]:
    """Ответ SOAP-сервиса: XML ядро не читает как лог, поэтому у теста только секция HTTP
    (ответ JSON был бы ещё и записью лога — её сигнатура берёт)."""
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">\n'
            f'  <soap:Header><rqUID>{request}</rqUID></soap:Header>\n'
            '  <soap:Body><soap:Fault>\n'
            f'    <faultcode>{code}</faultcode>\n'
            f'    <faultstring>{text}</faultstring>\n'
            '  </soap:Fault></soap:Body>\n'
            '</soap:Envelope>\n')
    return ("Response", body, "application/xml")


def same_assert_http_causes() -> Case:
    """Один ассерт кода ответа, причина — только в HTTP-ответе SOAP, лога сервиса нет.

    Сигнатура HTTP-ответ не берёт (как и gate по логу), поэтому у «счёт закрыт» и «не хватает
    средств» за ``expected 200 but was 500`` она одна, а разводит кластеры шаг: запись о
    закрытом счёте подсказывается второму кластеру как точная (``signature_shared_wrong`` 1,
    ``kb_offers_wrong`` 1 — граница сигнатуры, модель видит ответ сама). Разбирать его по ней
    без модели нельзя: ответ не тот, что видел инженер (ревью Codex, ``kb_auto_wrong`` 1 → 0).
    Корреляция у каждого запроса своя и отпечаток не меняет.
    """
    builder = LaunchBuilder(6142, "Payments nightly")
    test_class = "ru.bank.payments.TransferApiTest"
    status = "Transfer status expected:<200> but was:<500>"
    closed = ("ACCOUNT_CLOSED", "Счёт списания закрыт")
    groups = [
        ("transfer-closed", "payments-account-closed", status, "Перевести со счёта клиента",
         closed, ("transferFromSaving", "transferFromCard")),
        ("transfer-funds", "payments-no-funds", status, "Оплатить услугу со счёта",
         ("NO_FUNDS", "Недостаточно средств на счёте"), ("payMobile", "payInternet")),
        ("deposit-closed", "payments-account-closed",
         "Deposit close status expected:<200> but was:<500>", "Закрыть вклад клиента",
         closed, ("closeDeposit", "closeSavingDeposit")),
    ]
    request = 0
    for group, cause, message, step, (code, text), methods in groups:
        for index, method in enumerate(methods):
            request += 1
            builder.add_failure(
                group, cause=cause, category="данные", name=method,
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 30 + index * 11),
                step=step, logs=[_payment_response(code, text, f"rq-{7300 + request}")],
                evidence=[text],
            )
    return builder.build("same_assert_http_causes")


# ---------------------------------------------------------------------------
# Хосты и локаторы в сообщении
# ---------------------------------------------------------------------------

def _connect_failure(host: str, address: str) -> tuple[str, str]:
    message = f"java.net.ConnectException: Failed to connect to {host}/{address}"
    return message, java_trace(message, [
        "okhttp3.internal.connection.RealConnection.connectSocket(RealConnection.kt:297)",
        "ru.company.platform.HttpGateway.call(HttpGateway.java:64)",
    ])


def _selenium_failure(selector: str) -> tuple[str, str]:
    message = (
        "org.openqa.selenium.NoSuchElementException: no such element: Unable to locate "
        f'element: {{"method":"css selector","selector":"{selector}"}}\n'
        "  (Session info: chrome=129.0.6668.58)\n"
        "For documentation on this error, please visit: https://www.selenium.dev/documentation/"
        "webdriver/troubleshooting/errors#no-such-element-exception"
    )
    return message, java_trace(message.split("\n", 1)[0], [
        "org.openqa.selenium.remote.RemoteWebDriver.findElement(RemoteWebDriver.java:350)",
        "ru.company.ui.UiActions.click(UiActions.java:41)",
    ])


def _playwright_failure(locator: str) -> tuple[str, str]:
    message = (f"TimeoutError: locator.fill: Timeout 15000ms exceeded.\nCall log:\n"
               f"  - waiting for {locator}")
    return message, (f"{message}\n    at CheckoutPage.fillField "
                     "(/e2e/pages/checkout.page.ts:41:22)\n")


def resources_split() -> Case:
    """Один шаг и общий helper в трейсе; отличаются хост или локатор — разные проблемы."""
    builder = LaunchBuilder(5114, "Platform and checkout")
    groups: list[tuple[str, str, str, str, str, tuple[str, str], str, tuple[str, ...]]] = [
        ("conn-inventory", "inventory-service-down", "окружение", "Вызвать внутренний сервис",
         "ru.company.platform.PlatformTest", _connect_failure("inventory.svc", "10.2.0.7:8080"),
         "Failed to connect to inventory.svc", ("stockLevels", "stockReserve")),
        ("conn-loyalty", "loyalty-service-down", "окружение", "Вызвать внутренний сервис",
         "ru.company.platform.PlatformTest", _connect_failure("loyalty.svc", "10.2.0.9:8080"),
         "Failed to connect to loyalty.svc", ("loyaltyBalance", "loyaltyAccrue")),
        ("ui-promo-banner", "promo-banner-removed", "тест", "Оформить заказ",
         "ru.company.ui.CheckoutUiTest", _selenium_failure("#promo-banner"),
         '"selector":"#promo-banner"', ("promoHome", "promoCart")),
        ("ui-gift-wrap", "gift-wrap-flag-off", "данные", "Оформить заказ",
         "ru.company.ui.CheckoutUiTest", _selenium_failure("#gift-wrap-toggle"),
         '"selector":"#gift-wrap-toggle"', ("giftWrapCheckout", "giftWrapProfile")),
        ("pw-coupon", "coupon-field-hidden", "тест", "Заполнить форму оформления",
         "storefront.checkout.CheckoutSpec", _playwright_failure("getByTestId('coupon-input')"),
         "waiting for getByTestId('coupon-input')", ("applyCoupon", "applyCouponTwice")),
        ("pw-email", "email-label-renamed", "тест", "Заполнить форму оформления",
         "storefront.checkout.CheckoutSpec", _playwright_failure("getByLabel('Email')"),
         "waiting for getByLabel('Email')", ("guestEmail", "guestEmailRetry")),
    ]
    for group, cause, category, step, test_class, (message, trace), evidence, methods in groups:
        for method in methods:
            builder.add_failure(
                group, cause=cause, category=category, name=method,
                full_name=f"{test_class}.{method}", message=message, trace=trace, step=step,
                evidence=[evidence], status="broken",
            )
    return builder.build("resources_split")


CATALOG_CONSOLE_LOG = (
    "2026-10-03 10:03:00 [INFO] http://shop.test/catalog - navigation started\n"
    "2026-10-03 10:03:01 [SEVERE] http://shop.test/static/catalog.js 1:20451 Uncaught "
    "TypeError: Cannot read properties of undefined (reading 'items')\n"
)


def resources_one_problem() -> Case:
    """Разные хосты, селекторы или значения в сообщении — но одна проблема (контроли).

    Реплики одного сервиса, разные элементы страницы, не загрузившейся из-за одной ошибки
    скрипта (общий консольный лог), заблокированные тестовые пользователи, сбой DNS стенда.
    """
    builder = LaunchBuilder(5115, "Stage smoke")
    for index, method in enumerate(("ordersList", "ordersCreate", "ordersCancel"), 1):
        message, trace = _connect_failure(f"orders-{index}.svc", f"10.3.0.{index}:8080")
        builder.add_failure(
            "orders-replicas-down", cause="orders-deploy-failed", category="окружение",
            name=method, full_name=f"ru.company.platform.OrdersTest.{method}", message=message,
            trace=trace, step="Вызвать сервис заказов", evidence=["Failed to connect to orders-"],
            status="broken",
        )
    for selector, method in ((".product-card", "catalogCards"), (".filters-panel", "catalogFilters"),
                             ("#sort-select", "catalogSort")):
        message, trace = _selenide(selector, "CatalogPage", method)
        builder.add_failure(
            "catalog-js-error", cause="catalog-bundle-broken", category="приложение",
            name=method, full_name=f"ru.company.ui.CatalogPageTest.{method}", message=message,
            trace=trace, step="Открыть каталог", log=CATALOG_CONSOLE_LOG,
            log_name="browser-console.log",
            evidence=["Uncaught TypeError: Cannot read properties of undefined (reading 'items')"],
            status="broken",
        )
    for index, (user, method) in enumerate((("ivanov", "loginBuyer"), ("petrova", "loginSeller"),
                                            ("sidorenko", "loginAdmin"))):
        message = f"Login failed: user '{user}' is locked after 3 failed attempts"
        builder.add_failure(
            "users-locked", cause="test-users-locked", category="данные", name=method,
            full_name=f"ru.company.auth.LoginTest.{method}", message=message,
            trace=java_trace(f"ru.company.auth.LoginException: {message}",
                             [f"ru.company.auth.LoginTest.{method}(LoginTest.java:{18 + index})"]),
            step="Войти в систему", evidence=["is locked after 3 failed attempts"],
        )
    for host, method in (("payments.stage.local", "stagePayments"), ("cdn.stage.local", "stageCdn"),
                         ("auth.stage.local", "stageAuth")):
        message = f"java.net.UnknownHostException: {host}: Temporary failure in name resolution"
        builder.add_failure(
            "stage-dns-down", cause="stage-dns-down", category="окружение", name=method,
            full_name=f"ru.company.platform.StageTest.{method}", message=message,
            trace=java_trace(message, [
                "java.base/java.net.InetAddress$CachedLookup.get(InetAddress.java:988)",
                "ru.company.platform.HttpGateway.call(HttpGateway.java:64)"]),
            step="Вызвать сервис стенда", evidence=["Temporary failure in name resolution"],
            status="broken",
        )
    return builder.build("resources_one_problem")


# ---------------------------------------------------------------------------
# Бывший holdout v1 (перенесён в dev 2026-10-08, журнал — holdout_journal.md)
# ---------------------------------------------------------------------------

def _check_trace(message: str, test_class: str, method: str, line: int) -> str:
    return java_trace(f"org.opentest4j.AssertionFailedError: {message}", [
        "org.junit.jupiter.api.AssertionUtils.fail(AssertionUtils.java:55)",
        "org.junit.jupiter.api.AssertEquals.assertEquals(AssertEquals.java:150)",
        f"{test_class}.{method}({test_class.rsplit('.', 1)[-1]}.java:{line})",
    ])


OPTIMISTIC_LOCK_LOG = (
    "2026-10-04T08:15:00.010Z  INFO 777 --- [warehouse] [nio-8081-exec-3] "
    "c.e.w.ShipmentController : PATCH /shipments/88\n"
    "2026-10-04T08:15:00.042Z ERROR 777 --- [warehouse] [nio-8081-exec-3] "
    "c.e.w.ShipmentService : Shipment update rejected\n"
    "org.springframework.orm.ObjectOptimisticLockingFailureException: Row was updated or "
    "deleted by another transaction (or unsaved-value mapping was incorrect): "
    "[com.example.warehouse.Shipment#88]\n"
    "\tat com.example.warehouse.ShipmentService.update(ShipmentService.java:91)\n"
    "2026-10-04T08:15:00.050Z  INFO 777 --- [warehouse] [nio-8081-exec-3] "
    "c.e.w.ShipmentController : PATCH /shipments/88 -> 409\n"
)
DUPLICATE_BARCODE_LOG = (
    'ts=2026-10-04T08:16:00.001Z level=info msg="register item" barcode=4600000000017\n'
    'ts=2026-10-04T08:16:00.020Z level=error msg="item registration failed" '
    'err="barcode 4600000000017 already registered for item 5521"\n'
)


def same_conflict_two_causes() -> Case:
    """Одинаковый 409 у клиента: оптимистическая блокировка против дубля штрихкода."""
    builder = LaunchBuilder(6101, "Warehouse nightly")
    message = "expected: <200> but was: <409>"
    groups = [
        ("wh-409-lock", "shipment-optimistic-lock", OPTIMISTIC_LOCK_LOG, "warehouse.log",
         "Row was updated or deleted by another transaction",
         ("moveShipment", "splitShipment", "mergeShipment")),
        ("wh-409-barcode", "barcode-reused-by-fixture", DUPLICATE_BARCODE_LOG, "items.log",
         "barcode 4600000000017 already registered for item 5521",
         ("registerItem", "registerKit")),
    ]
    for group, cause, log, log_name, evidence, methods in groups:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category="приложение" if "lock" in group else "данные",
                name=method, full_name=f"ru.shop.warehouse.WarehouseTest.{method}",
                message=message,
                trace=_check_trace(message, "ru.shop.warehouse.WarehouseTest", method, 70 + index),
                step="Сохранить изменения склада", log=log, log_name=log_name,
                evidence=[evidence],
            )
    return builder.build("same_conflict_two_causes")


CELERY_LOG = (
    "[2026-10-04 08:20:00,001: INFO/MainProcess] Task delivery.route[9f1] received\n"
    "[2026-10-04 08:20:01,300: ERROR/ForkPoolWorker-2] Task delivery.route[9f1] raised "
    "unexpected: KeyError('zone_id')\n"
    "Traceback (most recent call last):\n"
    "  File \"/srv/delivery/routing.py\", line 118, in route\n"
    "    zone = zones[order['zone_id']]\n"
    "KeyError: 'zone_id'\n"
)
SYSLOG_LOG = (
    "Oct  4 08:21:00 courier-api[311]: INFO accepted courier shift 12\n"
    "Oct  4 08:21:02 courier-api[311]: CRITICAL redis connection lost: "
    "Error 111 connecting to redis-courier:6379. Connection refused.\n"
    "Oct  4 08:21:03 courier-api[311]: INFO retry scheduled\n"
)
PIPE_LEVEL_LOG = (
    "2026-10-04 08:22:00.100 | INFO  | loyalty.points | accrue points for order 19\n"
    "2026-10-04 08:22:00.130 | ERROR | loyalty.points | Points rule LR-5 has no rate for "
    "tier PLATINUM\n"
    "2026-10-04 08:22:00.140 | INFO  | loyalty.points | request finished\n"
)


def mixed_formats() -> Case:
    """Celery с Traceback, syslog с CRITICAL, уровень в ``| … |``."""
    builder = LaunchBuilder(6102, "Delivery and loyalty")
    groups = [
        ("mix-celery", "route-zone-missing", "приложение", "delivery", CELERY_LOG,
         "celery.log", "Delivery route for order 19 was not built", ["KeyError: 'zone_id'"]),
        ("mix-syslog", "courier-redis-down", "окружение", "courier", SYSLOG_LOG, "syslog",
         "Courier shift 12 status expected OPEN but was NEW",
         ["redis connection lost: Error 111 connecting to redis-courier:6379"]),
        ("mix-pipe", "loyalty-rate-missing", "данные", "loyalty", PIPE_LEVEL_LOG, "loyalty.log",
         "Loyalty points for order 19 expected 190 but was 0",
         ["Points rule LR-5 has no rate for tier PLATINUM"]),
    ]
    for group, cause, category, service, log, log_name, message, evidence in groups:
        test_class = f"ru.shop.{service}.{service.title()}FlowTest"
        for index, method in enumerate(("happyPath", "repeatPath")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_check_trace(message, test_class, method, 12 + index * 4),
                step=f"Пройти сценарий {service}", log=log, log_name=log_name, evidence=evidence,
            )
    return builder.build("mixed_formats")


def ui_and_network() -> Case:
    """Playwright-таймауты на разных страницах и недоступные сервисы."""
    builder = LaunchBuilder(6103, "Storefront e2e")
    pages = [
        ("pw-catalog", "catalog-filter-renamed", "тест", "[data-test=price-filter]",
         ("filterByPrice", "resetFilter")),
        ("pw-checkout", "checkout-iframe-blocked", "окружение", "iframe#payment-frame",
         ("payByCard", "payBySbp")),
    ]
    for group, cause, category, selector, methods in pages:
        message = (f"TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
                   f"waiting for locator('{selector}')")
        for method in methods:
            builder.add_failure(
                group, cause=cause, category=category, name=method,
                full_name=f"storefront.e2e.{group}.{method}", message=message,
                trace=f"{message}\n    at /e2e/{group}.spec.ts:24:18\n",
                step="Открыть витрину", evidence=[f"waiting for locator('{selector}')"],
                status="broken",
            )
    for group, cause, host, methods in (
        ("net-pricing", "pricing-dns", "pricing.internal", ("priceList", "priceHistory")),
        ("net-geo", "geo-dns", "geo.internal", ("geocode",)),
    ):
        message = f"java.net.UnknownHostException: {host}: Name or service not known"
        for method in methods:
            builder.add_failure(
                group, cause=cause, category="окружение", name=method,
                full_name=f"ru.shop.net.NetworkTest.{method}", message=message,
                trace=java_trace(message, ["java.base/java.net.InetAddress.getAllByName0"
                                           "(InetAddress.java:1534)"]),
                step="Вызвать внешний сервис", evidence=[f"{host}: Name or service not known"],
                status="broken",
            )
    return builder.build("ui_and_network")


def silent_and_step_only() -> Case:
    """Без данных и только с шагом: у каждой своя неизвестная причина."""
    builder = LaunchBuilder(6104, "Back office")
    for index, (name, step) in enumerate((
        ("archiveOrders", "Архивировать заказы"), ("rebuildCache", None),
        ("rotateKeys", "Ротировать ключи"), ("purgeTrash", None),
    )):
        builder.add_failure(
            f"bo-silent-{index + 1}", cause=None, name=name,
            full_name=f"ru.shop.backoffice.MaintenanceTest.{name}", step=step,
        )
    return builder.build("silent_and_step_only")


# ---------------------------------------------------------------------------
# Логи Go и Java
# ---------------------------------------------------------------------------

def _ndjson(*records: dict[str, object]) -> str:
    """JSON-лог: запись на строку, поля в порядке словаря."""
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


KLOG_ERROR_LOG = (
    "I1003 10:00:00.120456       1 controller.go:118] Starting EndpointSlice controller\n"
    "I1003 10:00:00.130112       1 shared_informer.go:311] Waiting for caches to sync\n"
    "E1003 10:00:01.204877       1 endpointslice_controller.go:302] \"Error syncing endpoint "
    "slices for service, retrying\" err=\"failed to update shop/cart-api-7xk2p EndpointSlice: "
    "the object has been modified\" service=\"shop/cart-api\"\n"
    "I1003 10:00:01.300554       1 controller.go:140] Endpoint slice sync finished\n"
)
KLOG_FATAL_LOG = (
    "I1003 10:05:00.000101       7 main.go:41] Loading tariff config from "
    "/etc/tariffs/config.yaml\n"
    "F1003 10:05:00.002345       7 main.go:57] Failed to parse tariff config: yaml: line 14: "
    "did not find expected key\n"
    "goroutine 1 [running]:\n"
    "k8s.io/klog/v2.stacks(0x1)\n"
    "\t/go/pkg/mod/k8s.io/klog/v2@v2.100.1/klog.go:860 +0x89\n"
    "main.main()\n"
    "\t/app/cmd/tariffs/main.go:57 +0x2f4\n"
)
GO_PANIC_NIL_LOG = (
    "2026/10/03 10:10:00 orders-api listening on :8080\n"
    "2026/10/03 10:10:02 POST /orders customer=4411\n"
    "panic: runtime error: invalid memory address or nil pointer dereference\n"
    "[signal SIGSEGV: segmentation violation code=0x1 addr=0x18 pc=0x6d4c1a]\n"
    "\n"
    "goroutine 54 [running]:\n"
    "example.com/orders/internal/pricing.(*Calculator).Total(0x0, {0xc0001a2000, 0x2, 0x2})\n"
    "\t/app/internal/pricing/calculator.go:73 +0x3a\n"
    "example.com/orders/internal/api.(*Handler).CreateOrder(0xc000126480, {0x8f2a40, "
    "0xc0002b80e0}, 0xc0002ac100)\n"
    "\t/app/internal/api/orders.go:118 +0x1c5\n"
    "net/http.HandlerFunc.ServeHTTP(...)\n"
    "\t/usr/local/go/src/net/http/server.go:2136 +0x29\n"
    "\n"
    "goroutine 1 [IO wait]:\n"
    "internal/poll.runtime_pollWait(0x7f1c2a5e8e58, 0x72)\n"
    "\t/usr/local/go/src/runtime/netpoll.go:343 +0x85\n"
    "2026/10/03 10:10:05 orders-api restarted\n"
)
GO_PANIC_MAP_LOG = (
    "2026/10/03 10:12:00 orders-api listening on :8080\n"
    "2026/10/03 10:12:01 POST /orders/bulk items=3\n"
    "panic: assignment to entry in nil map\n"
    "\n"
    "goroutine 61 [running]:\n"
    "example.com/orders/internal/bulk.(*Importer).Add(...)\n"
    "\t/app/internal/bulk/importer.go:44\n"
    "example.com/orders/internal/api.(*Handler).BulkOrders(0xc000126480, {0x8f2a40, "
    "0xc0002b81c0}, 0xc0002ac300)\n"
    "\t/app/internal/api/bulk.go:61 +0x2b1\n"
    "net/http.HandlerFunc.ServeHTTP(...)\n"
    "\t/usr/local/go/src/net/http/server.go:2136 +0x29\n"
    "2026/10/03 10:12:03 orders-api restarted\n"
)
GO_FATAL_ERROR_LOG = (
    "2026/10/03 10:15:00 cache warmup started\n"
    "fatal error: concurrent map writes\n"
    "\n"
    "goroutine 88 [running]:\n"
    "example.com/catalog/internal/cache.(*Store).Put(...)\n"
    "\t/app/internal/cache/store.go:29\n"
    "example.com/catalog/internal/cache.(*Warmer).run(0xc0000a4000)\n"
    "\t/app/internal/cache/warmer.go:52 +0x14d\n"
    "created by example.com/catalog/internal/cache.(*Warmer).Start in goroutine 1\n"
    "\t/app/internal/cache/warmer.go:31 +0x6a\n"
)
LOGRUS_PANIC_LOG = (
    'time="2026-10-03T10:20:00Z" level=info msg="payout worker started" worker=3\n'
    'time="2026-10-03T10:20:04Z" level=panic msg="payout ledger is inconsistent: balance '
    '-120.50 for account ACC-7731" worker=3\n'
)
ZAP_JSON_LOG = _ndjson(
    {"level": "info", "ts": 1759486200.101, "caller": "server/http.go:51",
     "msg": "shipping-api started", "addr": ":8080"},
    {"level": "error", "ts": 1759486203.512, "caller": "quote/client.go:96",
     "msg": "carrier quote failed", "carrier": "dpd",
     "error": "carrier dpd responded 503: quota exceeded for account SH-12",
     "stacktrace": "example.com/shipping/internal/quote.(*Client).Fetch\n"
                   "\t/app/internal/quote/client.go:96\n"
                   "example.com/shipping/internal/api.(*Handler).Quote\n"
                   "\t/app/internal/api/quote.go:40\n"
                   "net/http.HandlerFunc.ServeHTTP\n"
                   "\t/usr/local/go/src/net/http/server.go:2136"},
    {"level": "info", "ts": 1759486203.530, "caller": "server/http.go:77",
     "msg": "request finished", "path": "/quotes"},
)
ZEROLOG_NUMERIC_LOG = _ndjson(
    {"level": 1, "time": "2026-10-03T10:25:00Z", "order": "ORD-5521",
     "message": "points accrual started"},
    {"level": 3, "time": "2026-10-03T10:25:01Z", "order": "ORD-5521",
     "message": "points rule LP-9 references deleted tier GOLD_OLD"},
    {"level": 1, "time": "2026-10-03T10:25:01Z", "order": "ORD-5521",
     "message": "points accrual finished"},
)
SLOG_JSON_LOG = _ndjson(
    {"time": "2026-10-03T10:30:00.101Z", "level": "INFO", "msg": "reservation request",
     "sku": "SKU-88"},
    {"time": "2026-10-03T10:30:00.240Z", "level": "ERROR", "msg": "reservation failed",
     "sku": "SKU-88", "err": "warehouse WH-3 is closed for inventory count"},
    {"time": "2026-10-03T10:30:00.260Z", "level": "INFO", "msg": "request finished"},
)
LOGRUS_JSON_LOG = _ndjson(
    {"level": "info", "msg": "sending push", "time": "2026-10-03T10:35:00Z", "user": "u-102"},
    {"error": "fcm: registration token is not registered", "level": "error",
     "msg": "push delivery failed", "time": "2026-10-03T10:35:01Z", "user": "u-102"},
)

# (группа, причина, категория, сервис, сообщение теста, вложение (имя, текст, тип),
#  доказательства)
_GO_FORMATS: list[tuple[str, str, str, str, str, tuple[str, str, str], list[str]]] = [
    ("go-klog-error", "endpoint-slice-conflict", "окружение", "cart",
     "Cart API is unreachable after deploy",
     ("kube-controller.log", KLOG_ERROR_LOG, "text/plain"),
     ["failed to update shop/cart-api-7xk2p EndpointSlice"]),
    ("go-klog-fatal", "tariff-config-invalid", "окружение", "tariffs",
     "Tariff list expected 12 items but was 0",
     ("tariffs.log", KLOG_FATAL_LOG, "text/plain"),
     ["Failed to parse tariff config: yaml: line 14: did not find expected key"]),
    ("go-fatal-error", "catalog-cache-race", "приложение", "catalog",
     "Catalog page did not load within 10 seconds",
     ("catalog.log", GO_FATAL_ERROR_LOG, "text/plain"),
     ["fatal error: concurrent map writes"]),
    ("go-logrus-panic", "payout-ledger-broken", "данные", "payouts",
     "Payout status expected SENT but was PENDING",
     ("payouts.log", LOGRUS_PANIC_LOG, "text/plain"),
     ["payout ledger is inconsistent: balance -120.50 for account ACC-7731"]),
    ("go-zap-json", "carrier-quota-exceeded", "окружение", "shipping",
     "Shipping quote request failed with HTTP 502 Bad Gateway",
     ("shipping.log", ZAP_JSON_LOG, "text/plain"),
     ["carrier quote failed", "carrier dpd responded 503: quota exceeded for account SH-12"]),
    ("go-zerolog-numeric", "loyalty-tier-deleted", "данные", "loyalty",
     "Loyalty points for order ORD-5521 expected 55 but was 0",
     ("loyalty.ndjson", ZEROLOG_NUMERIC_LOG, "application/x-ndjson"),
     ["points rule LP-9 references deleted tier GOLD_OLD"]),
    ("go-slog-json", "warehouse-closed", "окружение", "inventory",
     "Reservation for SKU-88 expected RESERVED but was FAILED",
     ("inventory.log", SLOG_JSON_LOG, "text/plain"),
     ["warehouse WH-3 is closed for inventory count"]),
    ("go-logrus-json", "push-token-stale", "данные", "notify",
     "Push notification for user u-102 was not delivered",
     ("notify.json", LOGRUS_JSON_LOG, "application/json"),
     ["push delivery failed", "fcm: registration token is not registered"]),
]


def go_log_formats() -> Case:
    """Группа на формат лога Go; две паники — за одним ассертом в одном шаге."""
    builder = LaunchBuilder(6201, "Go services")
    for group, cause, category, service, message, attachment, evidence in _GO_FORMATS:
        test_class = f"ru.shop.{service}.{service.title()}ApiTest"
        for index, method in enumerate(("check", "checkAgain")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 20 + index * 10),
                step=f"Проверить сервис {service}", logs=[attachment], evidence=evidence,
            )
    panics = [
        ("go-panic-nil", "pricing-nil-calculator", GO_PANIC_NIL_LOG, ("createOrder", "createGiftOrder"),
         "panic: runtime error: invalid memory address or nil pointer dereference"),
        ("go-panic-map", "bulk-import-nil-map", GO_PANIC_MAP_LOG, ("bulkOrders", "bulkOrdersAgain"),
         "panic: assignment to entry in nil map"),
    ]
    for group, cause, log, methods, evidence_line in panics:
        for index, method in enumerate(methods):
            builder.add_failure(
                group, cause=cause, category="приложение", name=f"orders {method}",
                full_name=f"ru.shop.orders.OrdersApiTest.{method}", message=ASSERT_500,
                trace=_assert_trace(ASSERT_500, "ru.shop.orders.OrdersApiTest", method,
                                    40 + index * 10),
                step="Вызвать API заказов", log=log, log_name="orders-api.log",
                evidence=[evidence_line],
            )
    return builder.build("go_log_formats")


EXCEPTION_IN_THREAD_LOG = (
    "2026-10-03 10:40:00.100  INFO 4242 --- [           main] c.e.batch.BatchApplication   "
    "            : Starting BatchApplication using Java 21\n"
    "Exception in thread \"main\" java.lang.IllegalStateException: Failed to load "
    "ApplicationContext for batch job 'settlement'\n"
    "\tat org.springframework.boot.SpringApplication.run(SpringApplication.java:335)\n"
    "\tat com.example.batch.BatchApplication.main(BatchApplication.java:12)\n"
    "Caused by: org.springframework.beans.factory.BeanCreationException: Error creating bean "
    "with name 'settlementReader': Invocation of init method failed\n"
    "\tat org.springframework.beans.factory.support.AbstractAutowireCapableBeanFactory"
    ".initializeBean(AbstractAutowireCapableBeanFactory.java:1786)\n"
    "\t... 8 more\n"
    "Caused by: java.io.FileNotFoundException: /data/settlement/2026-10-03.csv (No such file "
    "or directory)\n"
    "\tat java.base/java.io.FileInputStream.open0(Native Method)\n"
    "\t... 14 more\n"
)
EXCEPTION_IN_THREAD_BARE_LOG = (
    "Exception in thread \"kafka-consumer-1\" org.apache.kafka.common.errors."
    "SerializationException: Error deserializing key/value for partition invoices-3 at offset "
    "1182\n"
    "\tat org.apache.kafka.clients.consumer.internals.CompletedFetch.parseRecord"
    "(CompletedFetch.java:331)\n"
    "\tat org.apache.kafka.clients.consumer.KafkaConsumer.poll(KafkaConsumer.java:1250)\n"
    "Caused by: com.fasterxml.jackson.databind.exc.InvalidFormatException: Cannot deserialize "
    "value of type `java.math.BigDecimal` from String \"12,50\": not a valid representation\n"
    "\tat com.fasterxml.jackson.databind.exc.InvalidFormatException.from"
    "(InvalidFormatException.java:67)\n"
    "\t... 9 more\n"
)
LOGSTASH_JSON_LOG = _ndjson(
    {"@timestamp": "2026-10-03T10:45:00.120+03:00", "@version": "1",
     "message": "Recalculating tariffs for region 77",
     "logger_name": "com.example.tariffs.TariffService", "thread_name": "scheduling-1",
     "level": "INFO", "level_value": 20000},
    {"@timestamp": "2026-10-03T10:45:02.410+03:00", "@version": "1",
     "message": "Tariff recalculation failed for region 77",
     "logger_name": "com.example.tariffs.TariffService", "thread_name": "scheduling-1",
     "level": "ERROR", "level_value": 40000,
     "stack_trace": "org.springframework.dao.CannotAcquireLockException: could not execute "
                    "statement; SQL [n/a]\n"
                    "\tat org.springframework.orm.jpa.vendor.HibernateJpaDialect."
                    "convertHibernateAccessException(HibernateJpaDialect.java:284)\n"
                    "\tat com.example.tariffs.TariffService.recalculate(TariffService.java:88)\n"
                    "\tSuppressed: java.lang.IllegalStateException: Transaction rollback failed\n"
                    "\t\tat com.example.tariffs.TxSupport.rollback(TxSupport.java:31)\n"
                    "Caused by: org.postgresql.util.PSQLException: ERROR: canceling statement "
                    "due to lock timeout\n"
                    "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse"
                    "(QueryExecutorImpl.java:2725)\n"
                    "\t... 37 more\n"},
)
ECS_JSON_LOG = _ndjson(
    {"@timestamp": "2026-10-03T07:48:00.010Z", "log.level": "INFO",
     "message": "Refund requested for payment P-9001", "ecs.version": "1.2.0",
     "service.name": "refunds", "log.logger": "com.example.refunds.RefundController",
     "process.thread.name": "http-nio-8080-exec-4"},
    {"@timestamp": "2026-10-03T07:48:00.090Z", "log.level": "ERROR",
     "message": "Refund processing failed", "ecs.version": "1.2.0",
     "service.name": "refunds", "log.logger": "com.example.refunds.RefundService",
     "process.thread.name": "http-nio-8080-exec-4",
     "error.type": "java.lang.IllegalArgumentException",
     "error.message": "Refund amount 1500.00 exceeds captured amount 1200.00",
     "error.stack_trace": "java.lang.IllegalArgumentException: Refund amount 1500.00 exceeds "
                          "captured amount 1200.00\n"
                          "\tat com.example.refunds.RefundPolicy.check(RefundPolicy.java:41)\n"
                          "\tat com.example.refunds.RefundService.refund(RefundService.java:67)\n"},
)
JSON_TEMPLATE_LOG = _ndjson(
    {"@version": 1, "@timestamp": "2026-10-03T10:50:00.100+03:00",
     "message": "Invoice export started", "level": "INFO",
     "logger_name": "com.example.invoices.ExportJob", "thread_name": "export-2"},
    {"@version": 1, "@timestamp": "2026-10-03T10:50:00.300+03:00",
     "message": "Invoice export failed", "level": "ERROR",
     "logger_name": "com.example.invoices.ExportJob", "thread_name": "export-2",
     "exception": {
         "exception_class": "java.nio.file.AccessDeniedException",
         "exception_message": "/mnt/exports/invoices-2026-10.xml",
         "stacktrace": "java.nio.file.AccessDeniedException: /mnt/exports/invoices-2026-10.xml\n"
                       "\tat java.base/sun.nio.fs.UnixException.translateToIOException"
                       "(UnixException.java:90)\n"
                       "\tat com.example.invoices.ExportJob.write(ExportJob.java:52)\n"}},
)
RUBY_LOGGER_LOG = (
    "I, [2026-10-03T10:58:00.100123 #41]  INFO -- : [req-9] Started POST \"/api/orders\"\n"
    "E, [2026-10-03T10:58:00.180456 #41] ERROR -- : [req-9] Orders::CreateJob failed: "
    "warehouse WH-9 has no slot for zone NORTH\n"
    "app/jobs/orders/create_job.rb:31:in `perform'\n"
    "I, [2026-10-03T10:58:00.190789 #41]  INFO -- : [req-9] Completed 500 Internal Server "
    "Error in 90ms\n"
)
DOTNET_CONSOLE_LOG = (
    "info: Microsoft.AspNetCore.Hosting.Diagnostics[1]\n"
    "      Request starting HTTP/1.1 POST http://billing-api/api/v1/invoices - 180\n"
    "fail: Microsoft.AspNetCore.Diagnostics.ExceptionHandlerMiddleware[1]\n"
    "      An unhandled exception has occurred while executing the request.\n"
    "      Billing.TariffException: Tariff T-14 has no price for region 77\n"
    "         at Billing.Tariffs.TariffService.Price(Int32 region) in "
    "/src/Billing/Tariffs/TariffService.cs:line 52\n"
    "info: Microsoft.AspNetCore.Hosting.Diagnostics[2]\n"
    "      Request finished HTTP/1.1 POST http://billing-api/api/v1/invoices - 500 - 41.5530ms\n"
)
_MDC = "[traceId=6f1c2a, spanId=91be, userId=5512, tenant=retail, requestId=r-77]"
LONG_MDC_LOG = (
    f"2026-10-03 10:55:00.100 [http-nio-8080-exec-9] {_MDC} INFO  "
    "c.e.subs.SubscriptionController - Renew subscription SUB-301\n"
    f"2026-10-03 10:55:00.180 [http-nio-8080-exec-9] {_MDC} ERROR "
    "c.e.subs.SubscriptionService - Renewal of SUB-301 failed: payment method PM-88 expired "
    "in 09/2026\n"
    f"2026-10-03 10:55:00.200 [http-nio-8080-exec-9] {_MDC} INFO  "
    "c.e.subs.SubscriptionController - Request finished\n"
)

_JAVA_FORMATS: list[tuple[str, str, str, str, str, tuple[str, str, str], list[str]]] = [
    ("java-exception-in-thread", "settlement-file-missing", "данные", "batch",
     "Settlement report for 2026-10-03 was not created",
     ("batch.log", EXCEPTION_IN_THREAD_LOG, "text/plain"),
     ["java.io.FileNotFoundException: /data/settlement/2026-10-03.csv (No such file or "
      "directory)"]),
    ("java-exception-bare", "invoice-amount-format", "данные", "invoices",
     "Invoice INV-1182 expected status POSTED but was NEW",
     ("consumer-stderr.log", EXCEPTION_IN_THREAD_BARE_LOG, "text/plain"),
     ["Cannot deserialize value of type `java.math.BigDecimal` from String \"12,50\""]),
    ("java-logstash-json", "tariff-lock-timeout", "окружение", "tariffs",
     "Tariffs for region 77 expected version 5 but was 4",
     ("tariffs.json.log", LOGSTASH_JSON_LOG, "text/plain"),
     ["Tariff recalculation failed for region 77",
      "PSQLException: ERROR: canceling statement due to lock timeout"]),
    ("java-ecs-json", "refund-over-capture", "данные", "refunds",
     "Refund for payment P-9001 expected REFUNDED but was FAILED",
     ("refunds.ndjson", ECS_JSON_LOG, "application/x-ndjson"),
     ["Refund amount 1500.00 exceeds captured amount 1200.00"]),
    ("java-json-template", "export-dir-readonly", "окружение", "exports",
     "Invoice export file invoices-2026-10.xml was not found",
     ("export.json", JSON_TEMPLATE_LOG, "application/json"),
     ["java.nio.file.AccessDeniedException: /mnt/exports/invoices-2026-10.xml"]),
    ("java-long-mdc", "payment-method-expired", "данные", "subscriptions",
     "Subscription SUB-301 expected ACTIVE but was SUSPENDED",
     ("subscriptions.log", LONG_MDC_LOG, "text/plain"),
     ["Renewal of SUB-301 failed: payment method PM-88 expired in 09/2026"]),
]
# В MDC лога — id запроса: из такого лога берутся только записи операции теста, а её id —
# из вложения-запроса (core.md, «Сбор данных»).
_JAVA_REQUESTS = {
    "java-long-mdc": [("Request", '{"headers": {"requestId": "r-77"}}', "application/json")],
}


def java_log_formats() -> Case:
    """Группа на формат лога Java: необработанное исключение потока, JSON, длинный MDC."""
    builder = LaunchBuilder(6202, "Java services")
    for group, cause, category, service, message, attachment, evidence in _JAVA_FORMATS:
        test_class = f"ru.shop.{service}.{service.title()}FlowTest"
        for index, method in enumerate(("verify", "verifyAgain")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 30 + index * 10),
                step=f"Проверить {service}", logs=[attachment, *_JAVA_REQUESTS.get(group, [])],
                evidence=evidence,
            )
    return builder.build("java_log_formats")


# Одинаковый шум во всех трёх логах: слово error вне уровня, пустое поле error при INFO,
# WARN с ошибкой, числовой уровень zerolog ниже ошибки.
LEVEL_NOISE_LOG = (
    "2026/10/03 11:00:00 INFO retry policy: on error wait 5s\n"
    "2026/10/03 11:00:00 WARN slow upstream response, error budget at 80%\n"
    'time=2026-10-03T11:00:01Z level=info msg="health ok" error=""\n'
    'time=2026-10-03T11:00:01Z level=warn msg="retrying upstream" err="unexpected EOF"\n'
    "I1003 11:00:01.000100       1 probe.go:30] error rate 0.0% over last minute\n"
    "W1003 11:00:01.000200       1 probe.go:35] error threshold is not configured\n"
) + _ndjson(
    {"level": "info", "msg": "error budget ok", "error": ""},
    {"level": "warn", "msg": "retrying", "error": "connection reset by peer"},
    {"level": 2, "message": "cache miss", "error": "key not found"},
)
_CONTROL_ERRORS = [
    ("ctl-logfmt", "credit-limit",
     'time=2026-10-03T11:00:02Z level=error msg="order rejected" '
     'err="credit limit exceeded for customer C-17"\n',
     "credit limit exceeded for customer C-17"),
    ("ctl-json", "warehouse-quota",
     _ndjson({"level": "error", "msg": "stock reservation failed",
              "error": "warehouse quota exhausted for SKU-77"}),
     "warehouse quota exhausted for SKU-77"),
    ("ctl-klog", "address-unresolved",
     "E1003 11:00:02.000300       1 geocode.go:88] Delivery address of order 905 cannot be "
     "geocoded: street not found\n",
     "Delivery address of order 905 cannot be geocoded: street not found"),
]


def log_level_controls() -> Case:
    """Один ассерт и шаг, разные ошибки лога и одинаковый шум без уровня ошибки."""
    builder = LaunchBuilder(6203, "Order controls")
    tail = "2026/10/03 11:00:03 INFO request finished\n"
    for group, cause, error, evidence_line in _CONTROL_ERRORS:
        for index in range(2):
            method = f"{group.split('-', 1)[1]}Order{index + 1}"
            builder.add_failure(
                group, cause=cause, category="данные", name=f"orders {method}",
                full_name=f"ru.shop.orders.OrderControlTest.{method}", message=ASSERT_500,
                trace=_assert_trace(ASSERT_500, "ru.shop.orders.OrderControlTest", method,
                                    50 + index * 10),
                step="Создать заказ", log=LEVEL_NOISE_LOG + error + tail,
                log_name="orders.log", evidence=[evidence_line],
            )
    return builder.build("log_level_controls")


def _collected(text: str, collector: str, second: int) -> str:
    """Лог через сборщик контейнера: время RFC3339 перед каждой строкой — ``docker logs -t``
    (9 знаков долей), ``kubectl logs --timestamps`` (без хвостовых нулей), файл CRI."""
    out = []
    for index, line in enumerate(text.splitlines()):
        fraction = f"{(index * 104_729 + 7) % 10**9:09d}"
        if collector == "kubectl":
            fraction = fraction.rstrip("0") or "0"
        prefix = f"2026-10-05T09:{second // 60:02d}:{second % 60:02d}.{fraction}Z "
        out.append(prefix + ("stderr F " if collector == "cri" else "") + line)
        second += index % 2
    return "\n".join(out) + "\n"


_COLLECTED = [
    # (группа, причина, категория, сборщик, сервис, сообщение, шаг, лог, строка-доказательство)
    ("collected-npe", "orders-customer-null", "приложение", "docker", "orders",
     ASSERT_500, "Создать заказ",
     "2026-10-05 09:14:02.311  INFO 1 --- [nio-8080-exec-3] r.s.orders.OrderController   : "
     "POST /api/orders cart=C-5531\n"
     "2026-10-05 09:14:02.348 ERROR 1 --- [nio-8080-exec-3] r.s.orders.OrderService      : "
     "order creation failed for cart C-5531\n"
     'java.lang.NullPointerException: Cannot invoke "Customer.getSegment()" because "customer" is null\n'
     "\tat ru.shop.orders.OrderService.applySegment(OrderService.java:118)\n"
     "\tat ru.shop.orders.OrderService.create(OrderService.java:64)\n"
     "Caused by: java.lang.IllegalStateException: customer profile not loaded\n"
     "\tat ru.shop.profile.ProfileCache.get(ProfileCache.java:27)\n"
     "\t... 87 more\n"
     "2026-10-05 09:14:02.351  INFO 1 --- [nio-8080-exec-3] r.s.orders.OrderController   : "
     "responded 500\n",
     'java.lang.NullPointerException: Cannot invoke "Customer.getSegment()" because "customer" is null'),
    ("collected-pool", "payments-db-pool", "окружение", "kubectl", "payments",
     "expected: <200> but was: <503>", "Оплатить заказ",
     "2026-10-05 09:15:10,004 INFO  [payment-worker-2] PaymentGateway - charge order=O-7781\n"
     "2026-10-05 09:15:40,019 ERROR [payment-worker-2] PaymentGateway - charge failed order=O-7781\n"
     "org.springframework.jdbc.CannotGetJdbcConnectionException: Failed to obtain JDBC Connection\n"
     "\tat org.springframework.jdbc.datasource.DataSourceUtils.getConnection(DataSourceUtils.java:84)\n"
     "\tat ru.shop.payments.ChargeRepository.save(ChargeRepository.java:33)\n"
     "Caused by: java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not "
     "available, request timed out after 30000ms.\n"
     "\tat com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)\n"
     "\t... 41 more\n",
     "HikariPool-1 - Connection is not available, request timed out after 30000ms."),
    ("collected-traceback", "stock-zone-missing", "данные", "cri", "stock",
     'expected: <"done"> but was: <"failed">', "Синхронизировать склад",
     "2026-10-05 09:16:21,502 INFO stock.sync: sync started zone=Z-14\n"
     "2026-10-05 09:16:21,690 ERROR stock.sync: zone sync failed\n"
     "Traceback (most recent call last):\n"
     '  File "/app/stock/sync.py", line 88, in sync_zone\n'
     '    capacity = layout["zone_capacity"][zone]\n'
     "KeyError: 'Z-14'\n"
     "2026-10-05 09:16:21,702 INFO stock.sync: sync finished status=failed\n",
     "KeyError: 'Z-14'"),
    # Своих записей нет (printStackTrace): границы — время сборщика, стек — продолжение.
    ("collected-bare", "reports-template-missing", "окружение", "docker", "reports",
     "expected: <201> but was: <500>", "Сформировать отчёт",
     "Report renderer ready on :8090\n"
     "Rendering report R-2208 for period 2026-09\n"
     "java.lang.IllegalStateException: template monthly-v2.xlsx not found in /templates\n"
     "\tat ru.shop.reports.TemplateStore.load(TemplateStore.java:51)\n"
     "\tat ru.shop.reports.Renderer.render(Renderer.java:19)\n"
     "Report R-2208 aborted\n",
     "java.lang.IllegalStateException: template monthly-v2.xlsx not found in /templates"),
]


def collector_prefixed_logs() -> Case:
    """Логи сервисов через сборщик контейнера: время RFC3339 перед каждой строкой, в том числе
    перед строкой исключения и кадрами — стек остаётся продолжением записи об ошибке."""
    builder = LaunchBuilder(6204, "Containers")
    for number, (group, cause, category, collector, service, message, step, log,
                 evidence) in enumerate(_COLLECTED):
        test_class = f"ru.shop.{service}.{service.title()}ContainerTest"
        for index, method in enumerate(("check", "checkAgain")):
            builder.add_failure(
                group, cause=cause, category=category, name=f"{service} {method}",
                full_name=f"{test_class}.{method}", message=message,
                trace=_assert_trace(message, test_class, method, 40 + index * 10),
                step=step, log=_collected(log, collector, 120 * number + 30 * index),
                log_name=f"{service}.log", evidence=[evidence],
            )
    return builder.build("collector_prefixed_logs")


# ---------------------------------------------------------------------------
# Большой прогон
# ---------------------------------------------------------------------------

BIG_GROUPS = 40
BIG_TESTS_PER_GROUP = 8
BIG_LOG_LINES = 1500  # ≈ 150 КБ на вложение


def _big_prefix(line: int) -> str:
    return f"2026-10-03 10:{line // 60 % 60:02d}:{line % 60:02d} [INFO] "


def big_launch() -> Case:
    """≥ 300 падений с длинными логами: лимиты памяти, размер заданий, время ``prepare``."""
    builder = LaunchBuilder(5199, "Full regression", first_result_id=10_000)
    for group_index in range(BIG_GROUPS):
        service = f"svc{group_index:02d}"
        error = f"{service.upper()}-E{group_index * 7 + 3}: state machine stuck in PENDING"
        message = f"Entity status in {service} expected DONE but was PENDING (group {group_index})"
        step = f"Шаг {group_index}: проверить {service}"
        for test_index in range(BIG_TESTS_PER_GROUP):
            log = (
                noise_lines(_big_prefix, BIG_LOG_LINES // 2,
                            lambda line: f"{service}: processed batch {line} in 12ms, "
                                         f"queue depth {line % 17}, request id r-{line:06d}")
                + f"2026-10-03 11:00:00 [ERROR] {service}: {error}\n"
                + java_trace(f"com.example.{service}.StuckException: {error}",
                             [f"com.example.{service}.Worker.run(Worker.java:{40 + test_index})"])
                + noise_lines(_big_prefix, BIG_LOG_LINES // 2,
                              lambda line: f"{service}: heartbeat {line} ok")
            )
            method = f"check{test_index}"
            test_class = f"ru.company.{service}.{service.title()}Test"
            builder.add_failure(
                f"big-{service}", cause=f"{service}-stuck", category="приложение",
                name=f"{service} {method}", full_name=f"{test_class}.{method}",
                message=message, trace=_assert_trace(message, test_class, method, 10 + test_index),
                step=step, log=log, evidence=[error],
            )
    return builder.build("big_launch", heavy=True)


# ---------------------------------------------------------------------------
# Общий лог сервиса: ошибки нескольких операций в одном вложении
# ---------------------------------------------------------------------------

def _service_line(time: str, thread: str, oper: str, level: str, text: str) -> str:
    return f"2026-10-03 11:{time} [{thread}] [operUID={oper}] {level} {text}\n"


SHARED_SERVICE_LOG = (
    _service_line("00:00.100", "exec-1", "op-ord-1001", "INFO ",
                  "c.e.orders.OrderController - Create order ORD-11")
    + _service_line("00:00.180", "exec-1", "op-ord-1001", "ERROR",
                    "c.e.orders.DiscountService - Discount rule DR-5 not found for order ORD-11")
    + _service_line("00:01.050", "exec-2", "op-pay-2001", "INFO ",
                    "c.e.payments.RefundController - Refund payment P-7")
    + _service_line("00:01.120", "exec-2", "op-pay-2001", "ERROR",
                    "c.e.payments.ProviderClient - Payment provider returned DECLINED for refund "
                    "of P-7")
    + _service_line("00:02.000", "exec-3", "op-ord-1002", "ERROR",
                    "c.e.orders.DiscountService - Discount rule DR-5 not found for order ORD-12")
    + _service_line("00:02.400", "exec-4", "op-pay-2002", "ERROR",
                    "c.e.payments.ProviderClient - Payment provider returned DECLINED for refund "
                    "of P-8")
    + _service_line("00:03.000", "exec-5", "op-misc-9001", "INFO ",
                    "c.e.health.HealthController - Health check OK")
)


def _request(oper: str, path: str) -> tuple[str, str, str]:
    """Вложение-запрос теста: в заголовках — id его операции."""
    body = (f'{{"method": "POST", "url": "https://shop.test{path}", '
            f'"headers": {{"operUID": "{oper}", "Content-Type": "application/json"}}}}')
    return ("Request", body, "application/json")


def shared_service_log() -> Case:
    """Один и тот же лог сервиса приложен ко всем тестам; в нём ошибки разных операций.

    Своя операция теста — по ``operUID`` из вложения-запроса. Разные падения (заказ без
    правила скидки и отказ провайдера при возврате) — две проблемы, хотя их логи
    одинаковые. У теста без запроса id операции неизвестен: чужие ошибки лога ему не
    принадлежат, своей группы у него нет.
    """
    builder = LaunchBuilder(5121, "Shop regression")
    tests = [
        ("orders-discount", "orders-discount-rule-missing", "данные", "createOrder",
         "ru.company.shop.OrderTest", "Order ORD-11 was not created: status code 500",
         "op-ord-1001", "/orders", "Discount rule DR-5 not found for order ORD-11"),
        ("orders-discount", "orders-discount-rule-missing", "данные", "createOrderWithCoupon",
         "ru.company.shop.OrderTest", "Order ORD-12 was not created: status code 500",
         "op-ord-1002", "/orders", "Discount rule DR-5 not found for order ORD-12"),
        ("refund-declined", "refund-provider-declined", "приложение", "refundPayment",
         "ru.company.shop.RefundTest", "Refund of payment P-7 failed: status code 502",
         "op-pay-2001", "/refunds", "Payment provider returned DECLINED for refund of P-7"),
        ("refund-declined", "refund-provider-declined", "приложение", "refundPartially",
         "ru.company.shop.RefundTest", "Refund of payment P-8 failed: status code 502",
         "op-pay-2002", "/refunds", "Payment provider returned DECLINED for refund of P-8"),
    ]
    for index, (group, cause, category, method, test_class, message, oper, path,
                evidence) in enumerate(tests):
        builder.add_failure(
            group, cause=cause, category=category, name=method,
            full_name=f"{test_class}.{method}", message=message,
            trace=_assert_trace(message, test_class, method, 20 + index * 6),
            log=SHARED_SERVICE_LOG, log_name="shop-service.log",
            logs=[_request(oper, path)], evidence=[evidence],
        )
    loyalty = "Loyalty points were not credited for order ORD-31"
    builder.add_failure(
        "loyalty-unknown", cause=None, category="неизвестно", name="creditLoyaltyPoints",
        full_name="ru.company.shop.LoyaltyTest.creditLoyaltyPoints", message=loyalty,
        trace=_assert_trace(loyalty, "ru.company.shop.LoyaltyTest", "creditLoyaltyPoints", 15),
        log=SHARED_SERVICE_LOG, log_name="shop-service.log",
    )
    return builder.build("shared_service_log")


def _business_line(time: str, oper: str, level: str, text: str) -> str:
    return f"2026-10-03 12:{time} [{level}] [operUID={oper}] {text}\n"


# Ошибка бизнес-ответа пишется на INFO: уровень не ошибка, ошибка — в полях ответа.
# Доказательство — сама запись: «errorMessage: …» секции HTTP без неё не скажет, чей это ответ.
_CLOSED = '"errorCode":6,"errorMessage":"счет закрыт"'
_NO_FUNDS = '"errorCode":12,"errorMessage":"недостаточно средств на счете списания"'
BUSINESS_ERROR_LOG = (
    _business_line("00:00.100", "op-tr-3001", "INFO", "c.e.transfer.TransferController - "
                   "Transfer request accepted")
    + _business_line("00:00.150", "op-tr-3001", "INFO", "c.e.transfer.AccountClient - Response "
                     f'{{"status":"REJECTED",{_CLOSED},"account":"40817810000000000301"}}')
    + _business_line("00:01.000", "op-tr-3002", "INFO", "c.e.transfer.AccountClient - Response "
                     f'{{"status":"REJECTED",{_CLOSED},"account":"40817810000000000302"}}')
    + _business_line("00:01.300", "op-tr-4001", "INFO", "c.e.transfer.LimitClient - Response "
                     f'{{"status":"REJECTED",{_NO_FUNDS}}}')
    + _business_line("00:02.000", "op-tr-4002", "INFO", "c.e.transfer.LimitClient - Response "
                     f'{{"status":"REJECTED",{_NO_FUNDS}}}')
    + _business_line("00:02.500", "op-tr-5001", "INFO", "c.e.transfer.AccountClient - Response "
                     '{"status":"OK","errorCode":0,"errorMessage":null}')
)


def info_business_error() -> Case:
    """Причина — ошибка бизнес-ответа в INFO-записи общего лога сервиса, а не ERROR.

    Сообщение ассерта у всех тестов одно («статус перевода REJECTED»); различает их только
    ``errorMessage`` записи своей операции (``operUID`` из вложения-запроса): закрытый счёт
    получателя и нехватка средств — две проблемы. Успешный ответ (``errorCode`` 0) — не ошибка.
    Похожесть логов (0.45) между порогами gate и log override — разделяет разный ``errorCode``.
    """
    builder = LaunchBuilder(5122, "Transfers regression")
    message = "Transfer status expected:<DONE> but was:<REJECTED>"
    tests = [
        ("transfer-account-closed", "transfer-account-closed", "данные", "transferToClosed",
         "op-tr-3001", _CLOSED),
        ("transfer-account-closed", "transfer-account-closed", "данные", "transferToClosedCard",
         "op-tr-3002", _CLOSED),
        ("transfer-no-funds", "transfer-no-funds", "данные", "transferOverLimit",
         "op-tr-4001", _NO_FUNDS),
        ("transfer-no-funds", "transfer-no-funds", "данные", "transferWholeBalance",
         "op-tr-4002", _NO_FUNDS),
    ]
    test_class = "ru.company.bank.TransferTest"
    for index, (group, cause, category, method, oper, evidence) in enumerate(tests):
        builder.add_failure(
            group, cause=cause, category=category, name=method,
            full_name=f"{test_class}.{method}", message=message,
            trace=_assert_trace(message, test_class, method, 30 + index * 7),
            log=BUSINESS_ERROR_LOG, log_name="transfer-service.log",
            logs=[_request(oper, "/transfers")], evidence=[evidence],
        )
    return builder.build("info_business_error")


# ---------------------------------------------------------------------------
# Бывший holdout v2 (перенесён в dev 2026-10-15, журнал — holdout_journal.md)
# ---------------------------------------------------------------------------

ESC = "\x1b"


def _json_line(fields: dict[str, Any], *, compact: bool = True) -> str:
    """Строка JSON-лога; порядок полей сохраняется."""
    separators = (",", ":") if compact else (", ", ": ")
    return json.dumps(fields, ensure_ascii=False, separators=separators) + "\n"


def _clock(seconds: int) -> str:
    """``HH:MM:SS`` от полуночи."""
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _testify(location: str, error: str) -> str:
    """Сообщение Go testify: ``Error Trace`` и многострочный ``Error``."""
    first, *rest = error.split("\n")
    lines = [f"\tError Trace:\t{location}", f"\tError:      \t{first}"]
    lines += [f"\t            \t{line}" for line in rest]
    return "\n".join(lines) + "\n"


def _jest_tobe(expected: str, received: str) -> str:
    """Сообщение Jest ``toBe``."""
    return ("expect(received).toBe(expected) // Object.is equality\n\n"
            f"Expected: {expected}\nReceived: {received}")


def _nunit(expected: str, actual: str) -> str:
    """Сообщение NUnit ``Assert.That(..., Is.EqualTo(...))``."""
    return f"  Expected: {expected}\n  But was:  {actual}\n"


def _rspec_eq(expected: str, got: str) -> str:
    """Сообщение RSpec ``eq``."""
    return f"\nexpected: {expected}\n     got: {got}\n\n(compared using ==)\n"


def _wdio_not_displayed(selector: str, timeout_ms: int) -> str:
    """Сообщение WebdriverIO ``waitForDisplayed``."""
    return f'element ("{selector}") still not displayed after {timeout_ms}ms'


def _ruby_log(pid: int, start: int, request: str, lines: Iterable[tuple[str, str]]) -> str:
    """Лог Ruby ``Logger`` (Rails) с тегом запроса."""
    out = []
    for offset, (level, text) in enumerate(lines):
        stamp = f"2026-10-04T{_clock(start + offset // 3)}.{120045 + offset * 6731:06d}"
        out.append(f"{level[0]}, [{stamp} #{pid}] {level:>5} -- : [{request}] {text}\n")
    return "".join(out)


# --- log_formats_more -------------------------------------------------------------------


def log_formats_more() -> Case:
    """Восемь форматов логов разных стеков; причина видна только в логе.

    На формат — своя проблема из двух тестов: zap JSON (Go), pino JSON (Node), Serilog
    (.NET), Ruby Logger (Rails), журнал PostgreSQL, klog (оператор Kubernetes), Logger
    Elixir, Monolog (Laravel). Сообщение теста — ассерт статуса или ожидания и причину не
    называет; у второго теста группы в логе другие идентификаторы и время.
    """
    builder = LaunchBuilder(7101, "regression 2026-10-04 #7101")

    # zap (Go), Go testify
    for index, (subscriber, case) in enumerate(((7701, "prepaid_to_postpaid"),
                                               (7702, "postpaid_to_prepaid"))):
        ts = 1759572000 + index * 40
        log = (
            f'{{"level":"info","ts":{ts}.101,"caller":"httpapi/server.go:64",'
            f'"msg":"request started","method":"POST",'
            f'"path":"/v1/subscribers/{subscriber}/plan"}}\n'
            f'{{"level":"info","ts":{ts}.118,"caller":"plan/switch.go:97",'
            f'"msg":"loading rating rules","plan":"UNLIM-30","region":77}}\n'
            f'{{"level":"error","ts":{ts}.233,"caller":"plan/switch.go:142",'
            f'"msg":"plan switch failed","subscriber":{subscriber},'
            f'"error":"rating rule RR-14 not found for plan UNLIM-30"}}\n'
            f'{{"level":"info","ts":{ts}.240,"caller":"httpapi/server.go:81",'
            f'"msg":"request finished","status":500,"duration":"139.2ms"}}\n'
        )
        builder.add_failure(
            "zap-rating-rule", cause="rating-rule-missing", category="данные",
            name=f"TestPlanSwitch/{case}",
            full_name=f"gitlab.acme.dev/telecom/e2e/plans.TestPlanSwitch/{case}",
            message=_testify(f"/builds/telecom/e2e/plans/switch_test.go:{57 + index * 31}",
                             "Not equal: \nexpected: 200\nactual  : 500"),
            step="Switch subscriber plan", log=log, log_name="tariff-engine.log",
            evidence=["rating rule RR-14 not found for plan UNLIM-30"])

    # pino (Node), Jest
    for index, flight in enumerate(("SU1402", "SU1403")):
        time = 1759572101000 + index * 52_317
        host = f"seatmap-svc-6b7c9d{index}f-q{index}x2p"
        error = {"type": "Error",
                 "message": "aircraft layout A321-NEO-v3 is missing in layouts cache",
                 "stack": "Error: aircraft layout A321-NEO-v3 is missing in layouts cache\n"
                          "    at LayoutStore.get (/srv/app/src/layouts.js:58:11)\n"
                          "    at SeatmapService.build (/srv/app/src/seatmap.js:24:32)"}
        log = (
            _json_line({"level": 30, "time": time, "pid": 18, "hostname": host,
                        "reqId": f"req-{index + 7}", "msg": "incoming request",
                        "req": {"method": "GET", "url": f"/v2/flights/{flight}/seatmap"}})
            + _json_line({"level": 50, "time": time + 14, "pid": 18, "hostname": host,
                          "reqId": f"req-{index + 7}", "err": error,
                          "msg": "seatmap build failed"})
            + _json_line({"level": 30, "time": time + 15, "pid": 18, "hostname": host,
                          "reqId": f"req-{index + 7}", "res": {"statusCode": 500},
                          "responseTime": 15.4, "msg": "request completed"})
        )
        message = _jest_tobe("200", "500")
        builder.add_failure(
            "pino-layout", cause="aircraft-layout-missing", category="данные",
            name=f"seatmap › returns seat map for flight {flight}",
            full_name=f"api-tests/seatmap.test.js#seatmap returns seat map for flight {flight}",
            message=message,
            trace=f"Error: {message}\n"
                  "    at Object.<anonymous> "
                  f"(/repo/api-tests/seatmap.test.js:{31 + index * 12}:28)",
            step="GET seat map", log=log, log_name="seatmap-svc.log",
            evidence=["aircraft layout A321-NEO-v3 is missing in layouts cache"])

    # Serilog (.NET), NUnit
    for index, (claim, policy) in enumerate((("CLM-55102", "POL-88-1043"),
                                             ("CLM-55117", "POL-88-1050"))):
        second = 36002 + index * 9
        log = (
            f"[{_clock(second)} INF] Request starting HTTP/1.1 POST http://claims-api/api/claims"
            f" - application/json {412 + index * 30}\n"
            f"[{_clock(second)} INF] Registering claim {claim} for policy {policy}\n"
            f"[{_clock(second)} ERR] Failed to register claim {claim}\n"
            "Npgsql.PostgresException (0x80004005): 23503: insert or update on table "
            '"claim_documents" violates foreign key constraint "fk_claim_documents_policy"\n'
            "   at Npgsql.Internal.NpgsqlConnector.ReadMessageLong(Boolean async, "
            "DataRowLoadingMode dataRowLoadingMode, Boolean readingNotifications)\n"
            "   at Insurance.Claims.ClaimRepository.AddDocumentsAsync(Claim claim) in "
            "/src/Insurance.Claims/ClaimRepository.cs:line 77\n"
            f"[{_clock(second + 1)} INF] Request finished HTTP/1.1 POST "
            "http://claims-api/api/claims - 500 0 - 84.1220ms\n"
        )
        method = ("RegisterClaimWithPhotos", "RegisterClaimWithWitness")[index]
        builder.add_failure(
            "serilog-claim-fk", cause="claim-documents-fk", category="приложение",
            name=method,
            full_name=f"Insurance.Tests.Claims.ClaimRegistrationTests.{method}",
            message=_nunit("201", "500"),
            trace=f"   at Insurance.Tests.Claims.ClaimRegistrationTests.{method}() in "
                  "/src/tests/Insurance.Tests/Claims/ClaimRegistrationTests.cs:line "
                  f"{48 + index * 21}\n",
            step="Register claim", log=log, log_name="claims-api.log",
            evidence=['violates foreign key constraint "fk_claim_documents_policy"'])

    # Ruby Logger (Rails), RSpec
    for index, (request, student) in enumerate((("8f2c1d0a", "S-1042"),
                                                ("b71e09c4", "S-1077"))):
        log = _ruby_log(41 + index, 36003 + index * 5, request, [
            ("INFO", 'Started POST "/api/v1/enrollments" for 172.22.4.15 at '
                     f"2026-10-04 {_clock(36003 + index * 5)} +0000"),
            ("INFO", "Processing by Api::V1::EnrollmentsController#create as JSON"),
            ("INFO", f'  Parameters: {{"student_id"=>"{student}", "cohort"=>"2026-B"}}'),
            ("ERROR", "Enrollments::CohortClosed: cohort 2026-B is closed for enrollment "
                      "since 2026-10-01"),
            ("INFO", "Completed 422 Unprocessable Entity in 61ms "
                     "(ActiveRecord: 12.4ms | Allocations: 5120)"),
        ])
        title = ("enrolls a student into an open cohort", "enrolls a transfer student")[index]
        builder.add_failure(
            "rails-cohort-closed", cause="cohort-closed", category="данные",
            name=f"Enrollments API POST /api/v1/enrollments {title}",
            full_name=f"spec/requests/enrollments_spec.rb[1:2:{index + 1}]",
            message=_rspec_eq("201", "422"),
            trace=f"./spec/requests/enrollments_spec.rb:{27 + index * 14}:in `block (3 levels) "
                  "in <top (required)>'\n",
            step="POST /api/v1/enrollments", log=log, log_name="enrollment.log",
            evidence=["cohort 2026-B is closed for enrollment"])

    # журнал PostgreSQL, pytest + httpx
    for index, (slot, pid) in enumerate(((4411, 2231), (4418, 2274))):
        url = f"http://appointments-api:8000/v1/slots/{slot}/book"
        error = (f"httpx.HTTPStatusError: Server error '500 Internal Server Error' for url '{url}'"
                 "\nFor more information check: "
                 "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
        stamp = f"2026-10-04 10:00:{4 + index * 6:02d}"
        log = (
            f"{stamp}.512 UTC [{pid}] LOG:  connection authorized: user=clinic database=clinic\n"
            f"{stamp}.731 UTC [{pid}] ERROR:  deadlock detected\n"
            f"{stamp}.731 UTC [{pid}] DETAIL:  Process {pid} waits for ShareLock on transaction "
            f"{99812 + index}; blocked by process {pid + 9}.\n"
            f"\tProcess {pid + 9} waits for ShareLock on transaction {99810 + index}; "
            f"blocked by process {pid}.\n"
            f"{stamp}.731 UTC [{pid}] HINT:  See server log for query details.\n"
            f"{stamp}.731 UTC [{pid}] STATEMENT:  UPDATE doctor_slots SET state = 'BOOKED' "
            f"WHERE slot_id = {slot}\n"
        )
        test = ("test_book_free_slot", "test_book_slot_with_referral")[index]
        builder.add_failure(
            "postgres-deadlock", cause="doctor-slots-deadlock", category="приложение",
            name=test, full_name=f"tests.api.test_booking#{test}",
            message=error,
            trace=f"tests/api/test_booking.py:{44 + index * 19}: in {test}\n"
                  "    response.raise_for_status()\n"
                  ".venv/lib/python3.12/site-packages/httpx/_models.py:829: in raise_for_status\n"
                  "    raise HTTPStatusError(message, request=request, response=self)\n"
                  f"E   {error}\n",
            step="Book appointment slot", log=log, log_name="postgres.log",
            evidence=["ERROR: deadlock detected",
                      "UPDATE doctor_slots SET state = 'BOOKED'"])

    # klog (оператор Kubernetes), Ginkgo/Gomega
    for index, device in enumerate(("dev-7712", "dev-7790")):
        stamp = f"I1004 10:0{index}:05"
        log = (
            f'{stamp}.100231       1 reconciler.go:88] "Reconciling Device" '
            f'device="fleet/{device}"\n'
            f'{stamp}.214502       1 firmware.go:52] "Rolling out firmware" '
            f'device="fleet/{device}" version="4.2.1"\n'
            f'E1004 10:0{index}:05.341870       1 controller.go:329] "Reconciler error" '
            'err="admission webhook \\"vfirmware.iot.example.com\\" denied the request: '
            f'firmware 4.2.1 is not signed" device="fleet/{device}"\n'
            f'{stamp}.902114       1 reconciler.go:88] "Reconciling Device" '
            f'device="fleet/{device}"\n'
        )
        title = ("upgrades a tracker to 4.2.1", "upgrades a gateway to 4.2.1")[index]
        builder.add_failure(
            "klog-firmware-unsigned", cause="firmware-not-signed", category="окружение",
            name=f"[It] Device firmware rollout {title}",
            full_name=f"iot-e2e/devices Device firmware rollout {title}",
            message="Timed out after 120.001s.\nExpected\n    <string>: Pending\n"
                    "to equal\n    <string>: Online",
            trace=f"/builds/iot/e2e/devices/rollout_test.go:{73 + index * 22}\n",
            step="Wait for device to come online", log=log, log_name="device-operator.log",
            evidence=["firmware 4.2.1 is not signed"])

    # Logger (Elixir/Phoenix), Spock
    for index, room in enumerate((88, 131)):
        request = ("F5x2kQ9", "F5x3Lm1")[index]
        log = (
            f"10:00:{6 + index * 3:02d}.204 request_id={request} [info] "
            f"POST /api/rooms/{room}/messages\n"
            f"10:00:{6 + index * 3:02d}.219 request_id={request} [error] "
            "** (FunctionClauseError) no function clause matching in Chat.Moderation.check/2\n"
            "    (chat 1.14.0) lib/chat/moderation.ex:12: "
            f"Chat.Moderation.check(%{{room_id: {room}}}, :premoderated)\n"
            "    (chat 1.14.0) lib/chat_web/controllers/message_controller.ex:31: "
            "ChatWeb.MessageController.create/2\n"
            f"10:00:{6 + index * 3:02d}.221 request_id={request} [info] Sent 500 in 17ms\n"
        )
        feature = ("posts a message to a premoderated room",
                   "posts an attachment to a premoderated room")[index]
        builder.add_failure(
            "elixir-moderation", cause="moderation-clause", category="приложение",
            name=feature, full_name=f"chat.RoomMessagesSpec.{feature}",
            message="Condition not satisfied:\n\nresp.status == 201\n|    |      |\n"
                    "|    500    false\n",
            trace=java_trace(
                "org.spockframework.runtime.ConditionNotSatisfiedError: Condition not satisfied:",
                [f"chat.RoomMessagesSpec.{feature}(RoomMessagesSpec.groovy:{42 + index * 17})"]),
            step="Send message to room", log=log, log_name="chat.log",
            evidence=["no function clause matching in Chat.Moderation.check/2"])

    # Monolog (Laravel), PHPUnit
    for index, listing in enumerate((3301, 3315)):
        log = (
            f'[2026-10-04 10:00:{7 + index * 4:02d}] testing.INFO: Publishing listing {listing} '
            f'{{"user_id":{17 + index}}} []\n'
            f"[2026-10-04 10:00:{7 + index * 4:02d}] testing.ERROR: Imagick extension is not "
            f'loaded {{"userId":{17 + index},"exception":"[object] (RuntimeException(code: 0): '
            "Imagick extension is not loaded at "
            '/var/www/html/app/Services/Thumbnailer.php:27)"} []\n'
        )
        method = ("test_owner_can_publish_listing", "test_agent_can_publish_listing")[index]
        builder.add_failure(
            "monolog-imagick", cause="imagick-missing", category="окружение",
            name=method, full_name=f"Tests\\Feature\\ListingPublishTest::{method}",
            message="Expected response status code [200] but received 500.\n"
                    "Failed asserting that 500 is identical to 200.",
            trace=f"/var/www/html/tests/Feature/ListingPublishTest.php:{35 + index * 16}\n",
            step="Publish listing", log=log, log_name="laravel.log",
            evidence=["Imagick extension is not loaded"])

    return builder.build("log_formats_more")


# --- log_traps_more ---------------------------------------------------------------------


def _nest_line(level: str, color: str, context: str, text: str, second: int) -> str:
    """Строка встроенного логгера NestJS с ANSI-цветами."""
    return (f"{ESC}[{color}m[Nest] 31  - {ESC}[39m10/04/2026, 10:13:{second:02d} AM "
            f"{ESC}[{color}m{level:>7}{ESC}[39m {ESC}[33m[{context}]{ESC}[39m "
            f"{ESC}[{color}m{text}{ESC}[39m\n")


def _telemetry_log(start: int, device: str) -> str:
    """Лог Go slog JSON на ~450 КБ: 3600 обычных строк, ошибка ровно посередине."""
    def head(line: int) -> str:
        return f'{{"time":"2026-10-04T{_clock(start + line // 20)}.{line % 20 * 50:03d}Z",'

    def batch(line: int) -> str:
        return (f'"level":"INFO","msg":"telemetry batch accepted",'
                f'"device":"trk-{line % 64:04d}","points":{100 + line % 37},'
                f'"lag_ms":{line % 13}}}')

    middle = _json_line({
        "time": f"2026-10-04T{_clock(start + 90)}.010Z", "level": "ERROR",
        "msg": "telemetry ingest stopped", "device": device,
        "err": "clickhouse: code: 241, message: Memory limit (total) exceeded: would use "
               "3.51 GiB (attempt to allocate chunk of 4194304 bytes), maximum: 3.46 GiB"})
    return (noise_lines(head, 1800, batch) + middle
            + noise_lines(lambda line: head(line + 1800), 1800, batch))


def log_traps_more() -> Case:
    """Настоящая ошибка рядом со строками, похожими на ошибку, и неудобные логи.

    Слово error в URL, полях и тексте INFO; WARN с исключением, после которого повтор
    удался; обработанное исключение на DEBUG; ANSI-цвета; CRLF; лог в сотни КБ с ошибкой в
    середине; несколько вложений, ошибка не в первом. На приём — своя проблема из двух тестов.
    """
    builder = LaunchBuilder(7102, "nightly #7102")

    # error в URL, полях и тексте INFO (structlog, pytest)
    for index, course in enumerate(("MATH-201", "MATH-305")):
        stamp = f"2026-10-04T10:10:{index * 20:02d}"
        log = (
            f"{stamp}.120412Z [info     ] request started                "
            "method=GET path=/api/v1/error-codes\n"
            f"{stamp}.131877Z [info     ] request finished               "
            "path=/api/v1/error-codes status=200\n"
            f"{stamp}.402113Z [info     ] submissions checked            "
            f"course={course} errors=[] error_count=0\n"
            f"{stamp}.511020Z [info     ] no errors found in gradebook   course={course}\n"
            f"{stamp}.512301Z [info     ] config loaded                  "
            "on_error=skip error_report_url=https://grades.qa.example.test/errors\n"
            f"{stamp}.733901Z [error    ] grade publication failed       "
            f"course={course} reason='grading scale GS-5 has overlapping ranges'\n"
            f"{stamp}.734550Z [info     ] request finished               "
            "path=/api/v1/grades/publish status=500\n"
        )
        test = ("test_publish_final_grades", "test_publish_retake_grades")[index]
        message = f"Failed: grades for {course} were not published within 30 s"
        builder.add_failure(
            "info-error-words", cause="grading-scale-overlap", category="данные",
            name=test, full_name=f"tests.test_grades#{test}", message=message,
            trace=f"tests/test_grades.py:{58 + index * 23}: in {test}\n"
                  "    pytest.fail(f\"grades for {course} were not published within 30 s\")\n"
                  f"E   {message}\n",
            step="Publish grades", log=log, log_name="grades-service.log",
            evidence=["grading scale GS-5 has overlapping ranges"])

    # WARN с исключением и удавшийся повтор (ECS JSON, Awaitility)
    for index, trip in enumerate(("TRP-301", "TRP-322")):
        stamp = f"2026-10-04T10:11:{index * 15:02d}"
        retry_error = ("java.util.concurrent.TimeoutException: Did not observe any item or "
                       "terminal signal within 2000ms in 'flatMap'\n\tat reactor.core.publisher."
                       "FluxTimeout$TimeoutMainSubscriber.handleTimeout(FluxTimeout.java:296)")
        log = (
            _json_line({"@timestamp": f"{stamp}.101Z", "log.level": "INFO",
                        "message": f"Route {trip} requested Central->Airport",
                        "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.104Z", "log.level": "WARN",
                          "message": "GTFS provider call failed, retrying (attempt 1/3)",
                          "error.type": "java.util.concurrent.TimeoutException",
                          "error.stack_trace": retry_error, "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.391Z", "log.level": "INFO",
                          "message": "GTFS provider responded on attempt 2",
                          "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.402Z", "log.level": "ERROR",
                          "message": f"Route {trip} rejected",
                          "error.type": "com.acme.transit.StaleFeedException",
                          "error.message": "GTFS feed 2026-10-02 is older than 24h",
                          "service.name": "timetable"})
        )
        method = ("plansRouteToAirport", "plansNightRoute")[index]
        exception = ("org.awaitility.core.ConditionTimeoutException: Condition with lambda "
                     "expression in com.acme.transit.TripPlannerIT that uses java.lang.String "
                     "was not fulfilled within 10 seconds.")
        builder.add_failure(
            "warn-retry-trap", cause="gtfs-feed-stale", category="окружение",
            name=method, full_name=f"com.acme.transit.TripPlannerIT.{method}",
            message=exception.split(": ", 1)[1],
            trace=java_trace(exception, [
                "org.awaitility.core.ConditionAwaiter.await(ConditionAwaiter.java:167)",
                "org.awaitility.core.ConditionFactory.until(ConditionFactory.java:985)",
                f"com.acme.transit.TripPlannerIT.{method}(TripPlannerIT.java:{64 + index * 18})",
            ]),
            step="Wait for planned route", log=log, log_name="timetable.json",
            evidence=["GTFS feed 2026-10-02 is older than 24h"])

    # обработанное исключение на DEBUG (ASP.NET Core console, xUnit)
    for index, booking in enumerate(("BK-9001", "BK-9017")):
        log = (
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[1]\n"
            "      Request starting HTTP/1.1 POST http://rentals-api/api/bookings "
            "- application/json 233\n"
            "dbug: Rentals.Pricing.SurchargeCalculator[0]\n"
            "      Surcharge lookup fell back to default\n"
            "      System.Collections.Generic.KeyNotFoundException: The given key "
            f"'AIRPORT-{('SVO', 'LED')[index]}' was not present in the dictionary.\n"
            "         at System.Collections.Generic.Dictionary`2.get_Item(TKey key)\n"
            "         at Rentals.Pricing.SurchargeCalculator.Lookup(String zone) in "
            "/src/Rentals.Pricing/SurchargeCalculator.cs:line 41\n"
            "info: Rentals.Bookings.BookingService[0]\n"
            f"      Pricing booking {booking}\n"
            "fail: Microsoft.AspNetCore.Diagnostics.ExceptionHandlerMiddleware[1]\n"
            "      An unhandled exception has occurred while executing the request.\n"
            "      System.InvalidOperationException: Vehicle class 'SUV-L' has no active "
            "rate card for 2026-10-04\n"
            "         at Rentals.Pricing.RateCards.Get(String vehicleClass, DateOnly day) in "
            "/src/Rentals.Pricing/RateCards.cs:line 58\n"
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[2]\n"
            "      Request finished HTTP/1.1 POST http://rentals-api/api/bookings "
            f"- 500 - application/problem+json {37 + index * 5}.2104ms\n"
        )
        method = ("CreatesBookingForSuv", "CreatesOneWayBookingForSuv")[index]
        builder.add_failure(
            "debug-handled-trap", cause="rate-card-missing", category="данные",
            name=method, full_name=f"Rentals.Tests.BookingApiTests.{method}",
            message="Assert.Equal() Failure\nExpected: 201\nActual:   500",
            trace=f"   at Rentals.Tests.BookingApiTests.{method}() in "
                  f"/src/tests/Rentals.Tests/BookingApiTests.cs:line {52 + index * 26}\n"
                  "--- End of stack trace from previous location ---\n",
            step="POST /api/bookings", log=log, log_name="rentals-api.log",
            evidence=["Vehicle class 'SUV-L' has no active rate card"])

    # ANSI-цвета (NestJS, Jest)
    for index, asset in enumerate(("sample-720p.mp4", "sample-1080p.mp4")):
        nest = _nest_line
        log = (
            nest("LOG", "32", "RoutesResolver", "ErrorsController {/api/errors}:", index)
            + nest("LOG", "32", "RouterExplorer", "Mapped {/api/errors/:id, GET} route", index)
            + nest("LOG", "32", "TranscodeService", f"Probing upload {asset}", index + 2)
            + nest("ERROR", "31", "TranscodeService",
                   "ffprobe exited with code 1: moov atom not found", index + 2)
            + nest("WARN", "33", "JobsService", "Job marked as FAILED, no retries left",
                   index + 2)
        )
        message = _jest_tobe('"READY"', '"FAILED"')
        builder.add_failure(
            "ansi-moov-atom", cause="fixture-video-corrupted", category="данные",
            name=f"transcoding › transcodes {asset}",
            full_name=f"tests/transcoding.e2e-spec.ts#transcoding transcodes {asset}",
            message=message,
            trace=f"Error: {message}\n    at Object.<anonymous> "
                  f"(/repo/tests/transcoding.e2e-spec.ts:{40 + index * 9}:35)",
            step="Wait for transcoding job", log=log, log_name="media-api.log",
            evidence=["moov atom not found"])

    # CRLF (Serilog file sink, NUnit)
    for index, envelope in enumerate(("ENV-3391", "ENV-3402")):
        lines = [
            f"2026-10-04 10:14:0{index}.123 +03:00 [INF] Envelope {envelope} sent to signing",
            f"2026-10-04 10:14:0{index}.380 +03:00 [ERR] Document signing failed for envelope "
            f"{envelope}",
            "System.Security.Cryptography.CryptographicException: The certificate chain was "
            "issued by an authority that is not trusted.",
            "   at ESign.Signing.CmsSigner.Sign(Byte[] content) in "
            "C:\\build\\src\\ESign.Signing\\CmsSigner.cs:line 88",
            f"2026-10-04 10:14:0{index}.402 +03:00 [INF] Envelope {envelope} state: Error",
        ]
        method = ("SignsEnvelopeWithQualifiedCertificate", "SignsEnvelopeByTwoParties")[index]
        builder.add_failure(
            "crlf-untrusted-chain", cause="signing-chain-untrusted", category="окружение",
            name=method, full_name=f"ESign.Tests.SigningTests.{method}",
            message=_nunit('"Signed"', '"Error"'),
            trace=f"   at ESign.Tests.SigningTests.{method}() in "
                  f"C:\\build\\tests\\ESign.Tests\\SigningTests.cs:line {33 + index * 15}\r\n",
            step="Sign envelope", log="\r\n".join(lines) + "\r\n", log_name="esign.log",
            evidence=["The certificate chain was issued by an authority that is not trusted."])

    # длинный лог, ошибка в середине (Go slog JSON, testify)
    for index, device in enumerate(("trk-0042", "trk-0107")):
        log = _telemetry_log(36900 + index * 600, device)
        test = ("TestTelemetry/track_is_stored", "TestTelemetry/geofence_alert")[index]
        builder.add_failure(
            "long-log-memory", cause="clickhouse-memory-limit", category="окружение",
            name=test, full_name=f"gitlab.acme.dev/fleet/e2e/telemetry.{test}",
            message=_testify(f"/builds/fleet/e2e/telemetry/track_test.go:{88 + index * 40}",
                             "Condition never satisfied"),
            step="Wait for telemetry points", log=log, log_name="telemetry-ingest.log",
            evidence=["Memory limit (total) exceeded"])

    # несколько вложений, ошибка не в первом (pytest, gunicorn)
    for index, result in enumerate(("LR-7781", "LR-7790")):
        request = (f"POST http://lab-gateway:8000/hl7/inbound\nContent-Type: application/hl7-v2"
                   f"\nX-Request-Id: {('5d1c', '9e04')[index]}-hl7\n\nMSH|^~\\&|LIS|QA|...\n")
        response = json.dumps({"result_id": result, "status": "PROCESSING", "errors": []})
        log = (
            f"[2026-10-04 10:16:0{index} +0000] [17] [INFO] Booting worker with pid: 17\n"
            f"[2026-10-04 10:16:0{index + 1} +0000] [17] [INFO] Accepted HL7 batch for {result}\n"
            f"[2026-10-04 10:16:0{index + 2} +0000] [17] [ERROR] HL7 message rejected: "
            "segment OBX-5 exceeds 64 KB limit\n"
            f"[2026-10-04 10:16:0{index + 2} +0000] [17] [INFO] Result {result} left in "
            "PROCESSING\n"
        )
        test = ("test_lab_result_with_pdf_report", "test_lab_result_with_scan")[index]
        message = f"Failed: lab result {result} still PROCESSING after 60 s"
        builder.add_failure(
            "third-attachment", cause="hl7-segment-limit", category="приложение",
            name=test, full_name=f"tests.lab.test_results#{test}",
            message=message,
            trace=f"tests/lab/test_results.py:{71 + index * 30}: in {test}\n"
                  f"E   {message}\n",
            step="Wait for lab result", log=request, log_name="request.http",
            logs=[("response.json", response, "application/json"),
                  ("lab-gateway.log", log, "text/plain")],
            evidence=["segment OBX-5 exceeds 64 KB limit"])

    return builder.build("log_traps_more")


# --- same_symptom_log_causes ------------------------------------------------------------


def same_symptom_log_causes() -> Case:
    """Одинаковые сообщение и шаг у разных проблем; причины различаются только логами.

    Шесть тестов pytest с одним и тем же ``HTTPStatusError`` 500 (три причины в логе сервиса
    на Rust) и шесть тестов Go с одним и тем же таймаутом клиента (три разные причины на
    сервере: блокировка в MySQL, медленный сосед, нехватка памяти и падение процесса).
    """
    builder = LaunchBuilder(7103, "release-candidate #7103")
    url = "http://policy-api:8080/v1/policies"
    status_error = (f"httpx.HTTPStatusError: Server error '500 Internal Server Error' for url "
                    f"'{url}'\nFor more information check: "
                    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
    policy_causes = [
        ("rust-rules-bundle", "uw-rules-bundle-missing", "окружение",
         "ERROR policy_api::issue: policy issue failed "
         "err=rules bundle uw-2026.10 is missing in bucket uw-rules",
         "rules bundle uw-2026.10 is missing in bucket uw-rules"),
        ("rust-overflow-panic", "premium-overflow", "приложение",
         "ERROR policy_api::panic: thread 'tokio-runtime-worker' panicked at "
         "src/pricing.rs:88:21:\nattempt to multiply with overflow",
         "attempt to multiply with overflow"),
        ("rust-sequence-max", "policy-number-sequence-exhausted", "окружение",
         "ERROR policy_api::numbering: policy issue failed err=error returned from "
         "database: nextval: reached maximum value of sequence \"policy_number_seq\" (999999)",
         'reached maximum value of sequence "policy_number_seq"'),
    ]
    tests = ["test_issue_policy_for_young_driver", "test_issue_policy_with_franchise",
             "test_issue_policy_for_country_house", "test_issue_policy_with_installments",
             "test_issue_policy_for_two_drivers", "test_issue_policy_for_electric_car"]
    for index, test in enumerate(tests):
        group, cause, category, error, evidence = policy_causes[index % 3]
        stamp = f"2026-10-04T10:20:{index * 7:02d}"
        log = (
            f"{stamp}.104512Z  INFO policy_api::http: request started method=POST "
            "uri=/v1/policies\n"
            f"{stamp}.121907Z  INFO policy_api::issue: issuing policy product=\"osago\"\n"
            f"{stamp}.188230Z {error}\n"
            f"{stamp}.188911Z  INFO policy_api::http: request finished status=500 "
            f"latency={84 + index}ms\n"
        )
        builder.add_failure(
            group, cause=cause, category=category,
            name=test, full_name=f"tests.policies.test_issue#{test}",
            message=status_error,
            trace="tests/policies/conftest.py:52: in issue_policy\n"
                  "    response.raise_for_status()\n"
                  f"E   {status_error}\n",
            step="Issue policy", log=log, log_name="policy-api.log",
            evidence=[evidence])

    timeout = _testify(
        "/builds/travel/e2e/holds/helpers_test.go:39",
        "Received unexpected error:\nPost \"http://booking-api:8080/v1/holds\": context "
        "deadline exceeded (Client.Timeout exceeded while awaiting headers)")
    hold_causes = [
        ("hold-lock-wait", "holds-lock-wait", "приложение",
         {"level": "ERROR", "msg": "hold insert failed",
          "err": "Error 1205 (HY000): Lock wait timeout exceeded; try restarting transaction"},
         "Lock wait timeout exceeded; try restarting transaction"),
        ("hold-channel-manager", "channel-manager-unreachable", "окружение",
         {"level": "ERROR", "msg": "availability check failed",
          "err": "Post \"http://channel-manager:9000/v2/availability\": dial tcp "
                 "172.24.3.17:9000: i/o timeout"},
         "dial tcp 172.24.3.17:9000: i/o timeout"),
        ("hold-oom", "booking-api-oom", "приложение", None,
         "fatal error: runtime: out of memory"),
    ]
    hold_tests = ["TestCreateHold/standard_room", "TestCreateHold/family_suite",
                  "TestCreateHold/two_rooms", "TestCreateHold/long_stay",
                  "TestCreateHold/late_checkin", "TestCreateHold/loyalty_rate"]
    for index, test in enumerate(hold_tests):
        group, cause, category, record, evidence = hold_causes[index % 3]
        stamp = f"2026-10-04T10:25:{index * 9:02d}"
        log = _json_line({"time": f"{stamp}.010Z", "level": "INFO", "msg": "hold requested",
                          "hotel": 501 + index, "nights": 2 + index % 3})
        if record is not None:
            log += _json_line({"time": f"{stamp}.950Z", **record, "hotel": 501 + index})
            log += _json_line({"time": f"{stamp}.951Z", "level": "INFO",
                               "msg": "request finished", "status": 500,
                               "duration": f"{50 + index}.1s"})
        else:
            log += ("fatal error: runtime: out of memory\n\nruntime stack:\n"
                    "runtime.throw({0x9f3c2a?, 0x0?})\n"
                    "\t/usr/local/go/src/runtime/panic.go:1023 +0x5c fp=0x7ffd5c1e8f30\n"
                    "runtime.sysMapOS(0xc04c000000, 0x4000000)\n"
                    "\t/usr/local/go/src/runtime/mem_linux.go:167 +0x11b\n")
        builder.add_failure(
            group, cause=cause, category=category,
            name=test, full_name=f"gitlab.acme.dev/travel/e2e/holds.{test}",
            message=timeout, step="Create hold", log=log, log_name="booking-api.log",
            evidence=[evidence])

    return builder.build("same_symptom_log_causes")


# --- hosts_and_locators -----------------------------------------------------------------


def hosts_and_locators() -> Case:
    """Один шаг и одна обёртка в стеке; проблемы различаются хостом или локатором.

    Python requests и Go net/http — недоступны два разных сервиса; WebdriverIO и Capybara —
    не найдены два разных элемента. Внутри стека всё остальное в сообщении и стеке совпадает
    (кроме адреса объекта у urllib3 и строки вызова в тесте).
    """
    builder = LaunchBuilder(7104, "smoke #7104")

    # Python requests через обёртку qa_kit/http.py
    hosts = [("tariffs-api", "tariffs-api-down"), ("numbering-api", "numbering-api-down")]
    for index in range(4):
        host, cause = hosts[index % 2]
        error = (
            f"requests.exceptions.ConnectionError: HTTPConnectionPool(host='{host}', port=8000):"
            " Max retries exceeded with url: /v1/config (Caused by NewConnectionError("
            f"'<urllib3.connection.HTTPConnection object at 0x7f3a2c1d{0x5E50 + index * 0x130:x}>: "
            "Failed to establish a new connection: [Errno 111] Connection refused'))")
        test = ("test_plan_list", "test_number_reserve", "test_plan_change",
                "test_number_release")[index]
        builder.add_failure(
            f"py-{host}", cause=cause, category="окружение",
            name=test, full_name=f"tests.smoke.test_api#{test}",
            message=error,
            trace=f"tests/smoke/test_api.py:{21 + index * 9}: in {test}\n"
                  "    config = api.get(\"/v1/config\")\n"
                  "qa_kit/http.py:48: in get\n    return self._send(\"GET\", path)\n"
                  "qa_kit/http.py:72: in _send\n"
                  "    response = self._session.request(method, self._url(path), timeout=10)\n"
                  f"E   {error}\n",
            step="Load service config", evidence=[f"host='{host}', port=8000"])

    # Go net/http через apitest.Client
    services = [("sim-provisioning", "172.25.1.14", "sim-provisioning-down"),
                ("porting-gw", "172.25.1.19", "porting-gw-down")]
    for index in range(4):
        host, address, cause = services[index % 2]
        test = ("TestSim/activate", "TestPorting/port_in", "TestSim/suspend",
                "TestPorting/port_out")[index]
        builder.add_failure(
            f"go-{host}", cause=cause, category="окружение",
            name=test, full_name=f"gitlab.acme.dev/telecom/e2e/smoke.{test}",
            message=_testify(
                f"/builds/telecom/e2e/apitest/client.go:88\n"
                f"/builds/telecom/e2e/smoke/smoke_test.go:{30 + index * 11}",
                f"Received unexpected error:\nGet \"http://{host}:7000/v1/config\": "
                f"dial tcp {address}:7000: connect: connection refused"),
            step="Load service config", evidence=[f"dial tcp {address}:7000"])

    # WebdriverIO через BasePage.tap
    selectors = [('[data-qa="plan-card"]', "plan-card-missing"),
                 ('[data-qa="roaming-toggle"]', "roaming-toggle-missing")]
    for index in range(4):
        selector, cause = selectors[index % 2]
        message = _wdio_not_displayed(selector, 10000)
        title = ("opens plan details", "enables roaming", "compares plans",
                 "disables roaming")[index]
        builder.add_failure(
            f"wdio-{cause}", cause=cause, category="тест",
            name=f"Plan settings {title}",
            full_name=f"tests/specs/plans.e2e.js#Plan settings {title}",
            message=message,
            trace=f"Error: {message}\n"
                  "    at BasePage.tap (/repo/tests/pageobjects/base.page.js:22:19)\n"
                  "    at Context.<anonymous> "
                  f"(/repo/tests/specs/plans.e2e.js:{18 + index * 13}:9)",
            step="Open plan settings", evidence=[selector])

    # Capybara через Pages::Base#press
    locators = [('Unable to find button "Book appointment" that is not disabled',
                 "book-button-missing", 'button "Book appointment"'),
                ('Unable to find css "#slot-picker .slot--free"', "free-slot-missing",
                 'css "#slot-picker .slot--free"')]
    for index in range(4):
        locator, cause, evidence = locators[index % 2]
        title = ("books a therapist", "books a dentist", "books a follow-up visit",
                 "books an online consultation")[index]
        builder.add_failure(
            f"capybara-{cause}", cause=cause, category="тест",
            name=f"Appointment booking {title}",
            full_name=f"spec/features/appointments_spec.rb[1:{index + 1}]",
            message=f"Capybara::ElementNotFound: {locator}",
            trace="./spec/support/pages/base_page.rb:14:in `press'\n"
                  f"./spec/features/appointments_spec.rb:{12 + index * 8}:in "
                  "`block (2 levels) in <top (required)>'\n",
            step="Choose a time slot", evidence=[evidence])

    return builder.build("hosts_and_locators")


# --- shared_assert_background -----------------------------------------------------------


_OTEL_BACKGROUND = (
    "fail: OpenTelemetry.Exporter.OtlpTraceExporter[0]\n"
    "      Exporter failed send data to collector to http://otel-collector:4317/ endpoint. "
    "Data will not be sent.\n"
    '      Grpc.Core.RpcException: Status(StatusCode="Unavailable", '
    'Detail="Error connecting to subchannel.")\n'
)
_FLAGS_BACKGROUND = (
    "fail: Unleash.Communication.UnleashApiClient[0]\n"
    "      Failed to fetch toggles from http://unleash:4242/api/client/features\n"
    "      System.Net.Http.HttpRequestException: Connection refused (unleash:4242)\n"
)


def shared_assert_background() -> Case:
    """Общий ассерт NUnit у разных проблем; причины — в логах среди фоновых ошибок.

    Три проблемы сервиса рецептов по три теста с одинаковым ``Expected: 200 But was: 500``;
    в логах — ошибки посторонних сервисов (экспорт телеметрии, флаги), у части логов разных
    проблем фон одинаков. Четвёртая проблема — одна ошибка, фон у каждого теста свой.
    """
    builder = LaunchBuilder(7105, "regression #7105")
    causes = {
        "formulary": ("formulary-missing-drug", "данные",
                      "Clinic.Prescriptions.FormularyException: Drug code N02BE01 is not in "
                      "the active formulary",
                      "Drug code N02BE01 is not in the active formulary"),
        "db-read": ("prescriptions-db-timeout", "окружение",
                    "Npgsql.NpgsqlException (0x80004005): Exception while reading from stream\n"
                    "       ---> System.TimeoutException: Timeout during reading attempt",
                    "Npgsql.NpgsqlException (0x80004005): Exception while reading from stream"),
        "concurrency": ("prescription-concurrency", "приложение",
                        "Microsoft.EntityFrameworkCore.DbUpdateConcurrencyException: The "
                        "database operation was expected to affect 1 row(s), but actually "
                        "affected 0 row(s); data may have been modified or deleted since "
                        "entities were loaded.",
                        "DbUpdateConcurrencyException: The database operation was expected to "
                        "affect 1 row(s), but actually affected 0 row(s)"),
        "dosage-format": ("dosage-format", "приложение",
                          "System.FormatException: The input string 'twice-a-day' was not in a "
                          "correct format.",
                          "The input string 'twice-a-day' was not in a correct format."),
    }
    otel, flags = _OTEL_BACKGROUND, _FLAGS_BACKGROUND
    plan = [
        ("formulary", "CreatesPrescriptionForParacetamol", otel),
        ("formulary", "CreatesPrescriptionForChild", otel),
        ("formulary", "RenewsPrescription", flags),
        ("db-read", "ListsActivePrescriptions", otel),
        ("db-read", "ListsPrescriptionHistory", otel),
        ("db-read", "FiltersPrescriptionsByDoctor", otel + flags),
        ("concurrency", "CancelsPrescription", flags),
        ("concurrency", "ExtendsPrescription", flags),
        ("concurrency", "ChangesPharmacy", otel),
        ("dosage-format", "ImportsPrescriptionFromHis", ""),
        ("dosage-format", "ImportsPrescriptionWithSchedule", flags + otel),
        ("dosage-format", "ImportsPrescriptionForCourse", flags),
    ]
    for index, (key, method, background) in enumerate(plan):
        cause, category, error, evidence = causes[key]
        error_lines = "\n".join(f"      {line.lstrip()}" for line in error.split("\n"))
        log = (
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[1]\n"
            f"      Request starting HTTP/1.1 POST http://prescriptions-api/api/v2/"
            f"prescriptions/{7300 + index} - application/json {180 + index * 7}\n"
            + background
            + "fail: Microsoft.AspNetCore.Diagnostics.ExceptionHandlerMiddleware[1]\n"
            "      An unhandled exception has occurred while executing the request.\n"
            f"{error_lines}\n"
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[2]\n"
            "      Request finished HTTP/1.1 POST http://prescriptions-api/api/v2/"
            f"prescriptions/{7300 + index} - 500 - application/problem+json "
            f"{41 + index * 3}.5530ms\n"
        )
        builder.add_failure(
            key, cause=cause, category=category,
            name=method, full_name=f"Clinic.Tests.Api.PrescriptionsTests.{method}",
            message=_nunit("200", "500"),
            trace=f"   at Clinic.Tests.Api.PrescriptionsTests.{method}() in "
                  f"/src/tests/Clinic.Tests/Api/PrescriptionsTests.cs:line {40 + index * 12}\n",
            step="Send prescription request", log=log, log_name="prescriptions-api.log",
            evidence=[evidence])

    return builder.build("shared_assert_background")


# --- one_problem_noisy ------------------------------------------------------------------


def _boarding_log(index: int, *, line: int, noise: str) -> str:
    """Лог pino сервиса посадочных талонов; меняются под, pid, id, время и строка стека."""
    pod = f"boarding-{('7f9c6d', '5b2e8a', '7f9c6d', '9d1f3c')[index % 4]}-{index:02d}k{index}x"
    pid = 17 + index * 3
    base = 1759573200000 + index * 61_733
    request = f"{index * 7919 + 101:x}-{index:04d}"
    common = {"pid": pid, "hostname": pod}
    noises = {
        "metrics": {"level": 50, "time": base + 3, **common, "msg": "metrics push failed",
                    "err": {"type": "Error", "message": "connect ETIMEDOUT 172.29.0.40:9091"}},
        "slow": {"level": 40, "time": base + 5, **common, "msg": "slow query",
                 "durationMs": 1840 + index, "table": "passengers"},
        "session": {"level": 50, "time": base + 2, **common, "msg": "session store ping failed",
                    "err": {"type": "Error", "message": "Connection is closed."}},
        "none": {},
    }
    error = ("Channel closed by server: 406 (PRECONDITION-FAILED) with message "
             "\"PRECONDITION_FAILED - inequivalent arg 'x-message-ttl' for queue "
             "'boarding-pass.issue' in vhost '/': received '60000' but current is '30000'\"")
    out = _json_line({"level": 30, "time": base, **common, "reqId": request,
                      "msg": "issue boarding pass", "passenger": f"P{4100 + index * 13}"})
    if noises[noise]:
        out += _json_line(noises[noise])
    out += _json_line({
        "level": 50, "time": base + 9 + index, **common, "reqId": request,
        "err": {"type": "Error", "message": error,
                "stack": f"Error: {error}\n    at BoardingPassPublisher.publish "
                         f"(/srv/app/dist/publisher.js:{line}:23)"},
        "msg": "failed to publish boarding pass job"})
    return out


def one_problem_noisy() -> Case:
    """Одна проблема (несовместимые аргументы очереди RabbitMQ) за тремя симптомами.

    Логи участников различаются подом, pid, id запроса, временем, строкой стека и
    посторонними ошибками; симптомы — ассерт Jest, исключение axios и ожидание
    WebdriverIO — разные группы с общей причиной. Один тест ассерта без лога.
    """
    builder = LaunchBuilder(7106, "nightly #7106")
    cause = "boarding-queue-args-mismatch"
    evidence = "inequivalent arg 'x-message-ttl' for queue 'boarding-pass.issue'"
    noises = ["metrics", "slow", "none", "session", "metrics", "slow", "session", "none",
              "metrics", "slow", "none"]

    assert_message = _jest_tobe('"ISSUED"', '"PENDING"')
    for index, flight in enumerate(("SU100", "SU202", "SU305", "SU410", "SU512")):
        has_log = index != 4
        builder.add_failure(
            "assert-pending", cause=cause, category="окружение",
            name=f"boarding pass › is issued after check-in on {flight}",
            full_name=f"tests/api/boarding.test.ts#boarding pass is issued after check-in "
                      f"on {flight}",
            message=assert_message,
            trace=f"Error: {assert_message}\n    at Object.<anonymous> "
                  f"(/repo/tests/api/boarding.test.ts:{27 + index * 15}:41)",
            step="Check boarding pass status",
            log=_boarding_log(index, line=41 + index % 2 * 3, noise=noises[index])
            if has_log else None,
            log_name="boarding.log",
            evidence=[evidence] if has_log else [])

    for index, kind in enumerate(("mobile", "kiosk", "web")):
        message = "AxiosError: Request failed with status code 503"
        builder.add_failure(
            "axios-503", cause=cause, category="окружение",
            name=f"boarding pass API › issues {kind} pass",
            full_name=f"tests/api/boarding-issue.test.ts#boarding pass API issues {kind} pass",
            message=message,
            trace=f"{message}\n    at settle (/repo/node_modules/axios/lib/core/settle.js:19:12)"
                  "\n    at IncomingMessage.handleStreamEnd "
                  "(/repo/node_modules/axios/lib/adapters/http.js:599:11)\n"
                  f"    at Object.<anonymous> (/repo/tests/api/boarding-issue.test.ts:"
                  f"{33 + index * 11}:22)",
            step="POST /v1/boarding-passes",
            log=_boarding_log(5 + index, line=44, noise=noises[5 + index]),
            log_name="boarding.log", evidence=[evidence])

    for index, flight in enumerate(("SU611", "SU733", "SU845")):
        message = _wdio_not_displayed('[data-qa="boarding-pass-qr"]', 15000)
        builder.add_failure(
            "wdio-qr", cause=cause, category="окружение",
            name=f"Online check-in shows boarding pass for {flight}",
            full_name=f"tests/specs/checkin.e2e.js#Online check-in shows boarding pass for "
                      f"{flight}",
            message=message,
            trace=f"Error: {message}\n    at async CheckinPage.openBoardingPass "
                  "(/repo/tests/pageobjects/checkin.page.js:48:9)",
            step="Open boarding pass",
            log=_boarding_log(8 + index, line=41 + index * 3, noise=noises[8 + index]),
            log_name="boarding.log", evidence=[evidence])

    return builder.build("one_problem_noisy")


# --- one_problem_resources --------------------------------------------------------------


def one_problem_resources() -> Case:
    """Одна проблема — разные ресурсы в сообщении.

    Реплики одного сервиса (имя пода в ошибке gRPC), разные студенты и курсы при одной
    блокировке таблицы, сбой DNS стенда по разным хостам в форматах Go, Node и .NET (на
    формат — группа, причина общая), разные элементы страницы, не загрузившейся из-за
    отсутствующего чанка JS.
    """
    builder = LaunchBuilder(7107, "regression #7107")

    for index, pod in enumerate(("rating-engine-6c9d8f7b5-x4k2m", "rating-engine-6c9d8f7b5-p7r9t",
                                 "rating-engine-6c9d8f7b5-z2w5q")):
        test = ("TestRating/local_call", "TestRating/sms_bundle", "TestRating/intl_call")[index]
        builder.add_failure(
            "replicas-cold-cache", cause="rating-cache-empty", category="приложение",
            name=test, full_name=f"gitlab.acme.dev/telecom/e2e/rating.{test}",
            message=_testify(f"/builds/telecom/e2e/rating/rating_test.go:{45 + index * 17}",
                             "Received unexpected error:\nrpc error: code = Internal desc = "
                             f"tariff cache is empty on {pod}"),
            step="Rate call record", evidence=["tariff cache is empty"])

    for index, (student, course) in enumerate((("S-2041", "PHYS-110"), ("S-2077", "CHEM-120"),
                                               ("S-2102", "PHYS-110"), ("S-2163", "BIO-130"))):
        log = (
            _json_line({"student": student, "course": course, "event": "allocating seat",
                        "level": "info", "timestamp": f"2026-10-04T10:40:{index * 4:02d}.120Z"},
                       compact=False)
            + _json_line({"student": student, "course": course,
                          "event": "seat allocation failed", "level": "error",
                          "timestamp": f"2026-10-04T10:40:{index * 4 + 3:02d}.122Z",
                          "error": "lock timeout: relation seat_quota is locked by migration "
                                   "0042_seat_quota"}, compact=False)
        )
        message = f"Failed: student {student} stayed WAITLISTED in course {course} after 30 s"
        test = f"test_enroll_into_course[{student}-{course}]"
        builder.add_failure(
            "data-waitlisted", cause="seat-quota-locked", category="окружение",
            name=test, full_name=f"tests.test_enrollment#{test}", message=message,
            trace=f"tests/test_enrollment.py:66: in test_enroll_into_course\nE   {message}\n",
            step="Wait for enrollment", log=log, log_name="enrollment-worker.log",
            evidence=["is locked by migration 0042_seat_quota"])

    dns = "qa-cluster-dns-down"
    for index, host in enumerate(("esign-api", "timesheet-api")):
        fqdn = f"{host}.qa.svc.cluster.local"
        test = ("TestContracts/send_for_signature", "TestTimesheets/submit_week")[index]
        builder.add_failure(
            "dns-go", cause=dns, category="окружение",
            name=test, full_name=f"gitlab.acme.dev/hr/e2e/flows.{test}",
            message=_testify(f"/builds/hr/e2e/flows/flows_test.go:{51 + index * 24}",
                             f"Received unexpected error:\nGet \"https://{fqdn}/v1/health\": "
                             f"dial tcp: lookup {fqdn} on 10.96.0.10:53: server misbehaving"),
            step="Call HR service",
            evidence=["on 10.96.0.10:53: server misbehaving"])
    for index, host in enumerate(("hr-docs", "vacation-api")):
        fqdn = f"{host}.qa.svc.cluster.local"
        message = f"Error: getaddrinfo EAI_AGAIN {fqdn}"
        title = ("uploads employment contract", "requests vacation")[index]
        builder.add_failure(
            "dns-node", cause=dns, category="окружение",
            name=f"HR portal › {title}",
            full_name=f"tests/hr-portal.test.js#HR portal {title}",
            message=message,
            trace=f"{message}\n    at GetAddrInfoReqWrap.onlookup [as oncomplete] "
                  "(node:dns:109:26)",
            step="Call HR service", evidence=["getaddrinfo EAI_AGAIN"])
    for index, host in enumerate(("payroll-calendar", "staff-directory")):
        fqdn = f"{host}.qa.svc.cluster.local"
        method = ("ReturnsWorkingDays", "FindsEmployeeByTabNumber")[index]
        exception = (f"System.Net.Http.HttpRequestException: Resource temporarily unavailable "
                     f"({fqdn}:443)")
        builder.add_failure(
            "dns-dotnet", cause=dns, category="окружение",
            name=method, full_name=f"Hr.Tests.Directory.LookupTests.{method}",
            message=exception,
            trace=f"{exception}\n ---> System.Net.Sockets.SocketException (11): Resource "
                  "temporarily unavailable\n   at System.Net.Http.HttpConnectionPool."
                  "ConnectToTcpHostAsync(String host, Int32 port, HttpRequestMessage "
                  "initialRequest, Boolean async, CancellationToken cancellationToken)\n",
            step="Call HR service", evidence=["Resource temporarily unavailable"])

    page_errors = json.dumps([{
        "type": "pageerror",
        "message": "ChunkLoadError: Loading chunk 712 failed.\n(missing: "
                   "https://rentals-web.qa.example.test/static/js/712.9c1e4b.chunk.js)"}])
    for index, element in enumerate(("vehicle-list", "pickup-date", "extras-panel",
                                     "rental-summary")):
        message = _wdio_not_displayed(f'[data-qa="{element}"]', 10000)
        title = ("lists vehicles", "picks a date", "adds child seat", "shows summary")[index]
        builder.add_failure(
            "page-not-loaded", cause="rentals-web-chunk-missing", category="окружение",
            name=f"Rental wizard {title}",
            full_name=f"tests/specs/rental-wizard.e2e.js#Rental wizard {title}",
            message=message,
            trace=f"Error: {message}\n    at async RentalWizard.open "
                  f"(/repo/tests/pageobjects/rental.page.js:{30 + index * 6}:9)",
            step="Open rental wizard", log=page_errors, log_name="page-errors.json",
            evidence=["ChunkLoadError: Loading chunk 712 failed."])

    return builder.build("one_problem_resources")


# --- signatures_and_kb ------------------------------------------------------------------


def signatures_and_kb() -> Case:
    """Близкие тексты разных проблем, общий и частичный признак причины в логе.

    Три проблемы с одним ассертом RSpec и похожими ошибками ``Tickets::IssueJob failed``;
    одна проблема с разными логами участников; причина с тремя симптомами (ассерт, таймаут
    клиента, элемент не найден) и общей строкой в логе; причина, у UI-симптома которой лога
    нет; посторонняя проблема с тем же ассертом, что у первого симптома общей причины.
    """
    builder = LaunchBuilder(7108, "regression #7108")
    issued = _rspec_eq('"issued"', '"pending"')
    close = [
        ("issue-hold-expired", "seat-hold-expired", "приложение",
         "Tickets::IssueJob failed: seat hold SH-4471 was released after 900 s"),
        ("issue-venue-map", "venue-map-version", "данные",
         "Tickets::IssueJob failed: venue map VM-12 version mismatch (expected 7, got 6)"),
        ("issue-pdf-renderer", "pdf-renderer-missing", "окружение",
         "Tickets::IssueJob failed: PDF renderer unavailable: wkhtmltopdf exited with "
         "status 127"),
    ]
    titles = ["issues a ticket for a concert", "issues a ticket for a play",
              "issues a ticket for a match", "issues a family ticket",
              "issues a VIP ticket", "issues a student ticket"]
    for index, title in enumerate(titles):
        group, cause, category, error = close[index % 3]
        request = f"{0x3a00 + index * 77:x}c1"
        log = _ruby_log(52, 39600 + index * 11, request, [
            ("INFO", 'Started POST "/api/v1/tickets" for 172.28.5.9'),
            ("INFO", "Processing by Api::V1::TicketsController#create as JSON"),
            ("INFO", "Completed 202 Accepted in 34ms (ActiveRecord: 7.9ms | Allocations: 3011)"),
            ("INFO", f"Performing Tickets::IssueJob (Job ID: {request}-job)"),
            ("ERROR", error),
        ])
        builder.add_failure(
            group, cause=cause, category=category,
            name=f"Tickets {title}", full_name=f"spec/requests/tickets_spec.rb[1:{index + 1}]",
            message=issued,
            trace=f"./spec/requests/tickets_spec.rb:{22 + index * 9}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="Wait for ticket issue", log=log, log_name="events-api.log",
            evidence=[error.split(": ", 1)[1]])

    refund_error = 'Refunds::FeeTable: fee table FT-2026 has no rule for channel "kiosk"'
    refund_logs = [
        [("INFO", 'Started POST "/api/v1/refunds" for 172.28.5.9'),
         ("ERROR", refund_error),
         ("INFO", "Completed 422 Unprocessable Entity in 18ms")],
        [("INFO", 'Started POST "/api/v1/refunds" for 172.28.5.14'),
         ("INFO", "Processing by Api::V1::RefundsController#create as JSON"),
         ("WARN", "Cache miss for fee_tables/FT-2026, loading from database"),
         ("INFO", "  FeeRule Load (2.1ms)  SELECT \"fee_rules\".* FROM \"fee_rules\" "
                  "WHERE \"fee_rules\".\"table_id\" = 2026"),
         ("ERROR", refund_error),
         ("INFO", "Completed 422 Unprocessable Entity in 41ms (ActiveRecord: 2.1ms)")],
        [("INFO", "Processing by Api::V1::RefundsController#create as JSON"),
         ("INFO", '  Parameters: {"ticket_id"=>"TK-88120", "channel"=>"kiosk"}'),
         ("ERROR", refund_error),
         ("ERROR", "Sentry: event dropped, rate limit exceeded"),
         ("INFO", "Completed 422 Unprocessable Entity in 26ms")],
    ]
    for index, lines in enumerate(refund_logs):
        title = ("refunds a kiosk ticket", "refunds a kiosk ticket with fee",
                 "refunds a kiosk group ticket")[index]
        builder.add_failure(
            "refund-fee-rule", cause="refund-fee-rule-missing", category="данные",
            name=f"Refunds {title}", full_name=f"spec/requests/refunds_spec.rb[1:{index + 1}]",
            message=_rspec_eq("200", "422"),
            trace=f"./spec/requests/refunds_spec.rb:{17 + index * 12}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="POST /api/v1/refunds",
            log=_ruby_log(53 + index, 39900 + index * 13, f"rf{index}{index * 31:03d}", lines),
            log_name="events-api.log",
            evidence=['fee table FT-2026 has no rule for channel "kiosk"'])

    replica = ('ActiveRecord::ConnectionNotEstablished (connection to server at '
               '"events-db-replica" (172.28.0.12), port 5432 failed: FATAL:  the database '
               'system is in recovery mode)')
    replica_evidence = "the database system is in recovery mode"
    symptoms = [
        ("replica-api-503", _rspec_eq("200", "503"), "GET /api/v1/events",
         ["lists upcoming events", "filters events by city"]),
        ("replica-read-timeout",
         "Net::ReadTimeout: Net::ReadTimeout with #<TCPSocket:(closed)>",
         "GET /api/v1/events/:id/seats", ["loads seat availability", "loads sector prices"]),
        ("replica-page-seats",
         'Capybara::ElementNotFound: Unable to find css ".event-seat--available"',
         "Open seat selection", ["selects a seat", "selects two adjacent seats"]),
    ]
    for group_index, (group, message, step, group_titles) in enumerate(symptoms):
        for index, title in enumerate(group_titles):
            number = group_index * 2 + index
            log = _ruby_log(60 + number, 40200 + number * 17, f"rp{number:02d}e", [
                ("INFO", f'Started GET "/api/v1/events?page={number + 1}" for 172.28.5.9'),
                ("ERROR", replica),
                ])
            builder.add_failure(
                group, cause="events-replica-recovery", category="окружение",
                name=f"Events {title}",
                full_name=f"spec/events/events_spec.rb[{group_index + 1}:{index + 1}]",
                message=message,
                trace=f"./spec/events/events_spec.rb:{15 + number * 10}:in `block (2 levels) "
                      "in <top (required)>'\n",
                step=step, log=log, log_name="events-api.log", evidence=[replica_evidence])

    for index, title in enumerate(("creates an event with a poster",
                                   "replaces an event poster")):
        log = _ruby_log(70 + index, 40500 + index * 7, f"pst{index}", [
            ("INFO", 'Started POST "/api/v1/events" for 172.28.5.9'),
            ("ERROR", "Aws::S3::Errors::NoSuchBucket (The specified bucket does not exist)"),
            ("INFO", "Completed 500 Internal Server Error in 112ms"),
        ])
        builder.add_failure(
            "poster-api-500", cause="posters-bucket-missing", category="окружение",
            name=f"Events admin {title}",
            full_name=f"spec/requests/events_admin_spec.rb[1:{index + 1}]",
            message=_rspec_eq("201", "500"),
            trace=f"./spec/requests/events_admin_spec.rb:{31 + index * 14}:in `block (2 levels) "
                  "in <top (required)>'\n",
            step="POST /api/v1/events", log=log, log_name="events-api.log",
            evidence=["The specified bucket does not exist"])
    for index, title in enumerate(("shows the poster on the event page",
                                   "shows the poster in the event list")):
        builder.add_failure(
            "poster-page-missing", cause="posters-bucket-missing", category="окружение",
            name=f"Event page {title}",
            full_name=f"spec/features/event_page_spec.rb[1:{index + 1}]",
            message='expected to find css "img.event-poster" but there were no matches',
            trace=f"./spec/features/event_page_spec.rb:{19 + index * 8}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="Open event page",
            evidence=['expected to find css "img.event-poster"'])

    for index, title in enumerate(("lists past events", "lists events of a venue")):
        log = _ruby_log(80 + index, 40800 + index * 5, f"ro{index}", [
            ("INFO", 'Started GET "/api/v1/events" for 172.28.5.9'),
            ("ERROR", "ReadOnlyModeError: writes are disabled by flag events.read_only "
                      "(audit log write rejected)"),
        ])
        builder.add_failure(
            "read-only-flag", cause="events-read-only-flag", category="окружение",
            name=f"Archive {title}", full_name=f"spec/requests/archive_spec.rb[1:{index + 1}]",
            message=_rspec_eq("200", "503"),
            trace=f"./spec/requests/archive_spec.rb:{11 + index * 9}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="GET /api/v1/events", log=log, log_name="events-api.log",
            evidence=["writes are disabled by flag events.read_only"])

    return builder.build("signatures_and_kb")


# --- retries_more -----------------------------------------------------------------------


def _fields(*, history_id: str | None = None, history_key: str | None = None,
            case_id: int | None = None, params: dict[str, str] | None = None,
            stand: str | None = None, start: int = 0) -> dict[str, Any]:
    """Поля связи TestOps для результата (только заданные) и время начала."""
    fields: dict[str, Any] = {"start": 1759575600000 + start * 1000}
    if history_id is not None:
        fields["historyId"] = history_id
    if history_key is not None:
        fields["historyKey"] = history_key
    if case_id is not None:
        fields["testCaseId"] = case_id
    if params is not None:
        fields["parameters"] = [{"name": key, "value": value} for key, value in params.items()]
    if stand is not None:
        fields["environment"] = [{"name": "stand", "value": stand}]
    return fields


def retries_more() -> Case:
    """Повторы pytest-rerunfailures: hidden-попытки и связь по полям TestOps.

    Прошёл после повтора; все попытки с той же ошибкой (разный номер заявки); попытка с
    другой ошибкой; параметризованный тест (попытки не смешиваются между параметрами); смена
    стенда между попытками (не повтор); ошибка попытки только в деталях результата; семь
    попыток; попытка без полей связи (не связывается); связь по ``historyKey`` без
    ``historyId`` и по ``testCaseId`` + параметрам + окружению.
    """
    builder = LaunchBuilder(7109, "telecom regression #7109")
    module = "tests.sim.test_lifecycle"

    def attempt(name: str, message: str | None, fields: dict[str, Any],
                status: str = "failed") -> int:
        return builder.add_result(name=name, status=status, full_name=f"{module}#{name}",
                                  message=message, hidden=True, extra=fields)

    # прошёл после повтора
    name = "test_activate_esim"
    attempt(name, "Failed: eSIM bundle download did not finish within 60 s",
            _fields(history_id="9a1f03", case_id=9101, stand="qa-1", start=0))
    passed = builder.add_result(name=name, status="passed", full_name=f"{module}#{name}",
                                extra=_fields(history_id="9a1f03", case_id=9101, stand="qa-1",
                                              start=70))
    builder.expect_passed_after_retry(passed)

    # все попытки с той же ошибкой, номер заявки меняется
    name = "test_port_in_number"
    attempts = [attempt(name, f"Failed: porting request PR-{5531 + n} rejected by donor "
                              "operator: code 4012 (subscriber data mismatch)",
                        _fields(history_id="9a1f04", case_id=9102, stand="qa-1",
                                start=100 + n * 40)) for n in range(2)]
    final = builder.add_failure(
        "porting-4012", cause="porting-subscriber-mismatch", category="данные",
        name=name, full_name=f"{module}#{name}",
        message="Failed: porting request PR-5533 rejected by donor operator: code 4012 "
                "(subscriber data mismatch)",
        step="Submit porting request",
        extra=_fields(history_id="9a1f04", case_id=9102, stand="qa-1", start=180),
        evidence=["code 4012 (subscriber data mismatch)"])
    builder.expect_attempts(final, [(attempts[0], True), (attempts[1], True)])

    # попытка с другой ошибкой
    name = "test_sim_provisioning"
    reset = attempt(name, "requests.exceptions.ChunkedEncodingError: (\"Connection broken: "
                          "ConnectionResetError(104, 'Connection reset by peer')\", "
                          "ConnectionResetError(104, 'Connection reset by peer'))",
                    _fields(history_id="9a1f05", case_id=9103, stand="qa-1", start=200),
                    status="broken")
    final = builder.add_failure(
        "sim-stuck-provisioning", cause=None, name=name, full_name=f"{module}#{name}",
        message="Failed: SIM 89701010000000004417 stayed in state PROVISIONING after 120 s",
        step="Wait for SIM activation",
        extra=_fields(history_id="9a1f05", case_id=9103, stand="qa-1", start=260))
    builder.expect_attempts(final, [(reset, False)])

    # параметризованный тест: у каждого параметра свои попытки
    for zone, history in (("EU", "9a1f06"), ("ASIA", "9a1f07")):
        name = f"test_roaming_rate[{zone}]"
        message = f"Failed: roaming rate for zone {zone} is 0.00, expected > 0"
        params = {"zone": zone}
        earlier = attempt(name, message, _fields(history_id=history, case_id=9104,
                                                 params=params, stand="qa-1", start=300))
        final = builder.add_failure(
            "roaming-zero-rate", cause="roaming-rates-not-loaded", category="данные",
            name=name, full_name=f"{module}#{name}", message=message, step="Get roaming rate",
            extra=_fields(history_id=history, case_id=9104, params=params, stand="qa-1",
                          start=330),
            evidence=["is 0.00, expected > 0"])
        builder.expect_attempts(final, [(earlier, True)])

    # смена стенда между попытками: это не повтор
    name = "test_data_bundle_counter"
    message = "Failed: data counter for bundle B-10GB shows 0 MB used after a 50 MB session"
    attempt(name, message, _fields(history_id="9a1f08", case_id=9105, stand="qa-2", start=400))
    final = builder.add_failure(
        "bundle-counter-zero", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Check data counter",
        extra=_fields(history_id="9a1f09", case_id=9105, stand="qa-1", start=460))
    builder.expect_attempts(final, [])

    # ошибка попытки — только в деталях результата
    name = "test_apn_settings_push"
    message = "Failed: APN settings 'internet.qa' were not pushed to device emulator-5554"
    silent = attempt(name, None, _fields(history_id="9a1f0a", case_id=9106, stand="qa-1",
                                         start=500))
    builder.add_detail(silent, message=message)
    final = builder.add_failure(
        "apn-not-pushed", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Push APN settings",
        extra=_fields(history_id="9a1f0a", case_id=9106, stand="qa-1", start=560))
    builder.expect_attempts(final, [(silent, True)])

    # семь попыток: первая с другой ошибкой
    name = "test_voicemail_greeting_upload"
    storage = ("httpx.HTTPStatusError: Server error '507 Insufficient Storage' for url "
               "'http://voicemail-api:8080/v1/greetings'")
    def voicemail(start: int) -> dict[str, Any]:
        return _fields(history_id="9a1f0b", case_id=9107, stand="qa-1", start=start)

    many = [attempt(name, "httpx.ConnectError: [Errno 111] Connection refused",
                    voicemail(600), status="broken")]
    many += [attempt(name, storage, voicemail(620 + n * 20)) for n in range(6)]
    final = builder.add_failure(
        "voicemail-507", cause="voicemail-storage-full", category="окружение",
        name=name, full_name=f"{module}#{name}", message=storage, step="Upload greeting",
        extra=voicemail(760),
        evidence=["507 Insufficient Storage"])
    builder.expect_attempts(final, [(many[0], False), *((item, True) for item in many[1:])])

    # попытка без полей связи: связать нечем
    name = "test_ussd_balance_request"
    message = "Failed: USSD *100# returned empty response"
    builder.add_result(name=name, status="failed", full_name=f"{module}#{name}",
                       message=message, hidden=True, extra={"start": 1759575600000 + 800_000})
    final = builder.add_failure(
        "ussd-empty", cause=None, name=name, full_name=f"{module}#{name}", message=message,
        step="Send USSD request",
        extra=_fields(history_id="9a1f0c", case_id=9108, stand="qa-1", start=840))
    builder.expect_attempts(final, [])

    # без historyId: связь по historyKey
    name = "test_call_forwarding_busy"
    message = "Failed: call to 79990001122 was not forwarded to voicemail on busy"
    keyed = attempt(name, message, _fields(history_key="cf-busy:qa-1", case_id=9109,
                                           stand="qa-1", start=900))
    final = builder.add_failure(
        "forwarding-busy", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Call busy subscriber",
        extra=_fields(history_key="cf-busy:qa-1", case_id=9109, stand="qa-1", start=950))
    builder.expect_attempts(final, [(keyed, True)])

    # без historyId и historyKey: testCaseId + параметры + окружение
    name = "test_number_block[lost]"
    message = "Failed: number 79990003344 is still active after block with reason 'lost'"
    params = {"reason": "lost"}
    by_case = attempt(name, message, _fields(case_id=9110, params=params, stand="qa-1",
                                             start=1000))
    builder.add_result(name="test_number_block[stolen]", status="passed",
                       full_name=f"{module}#test_number_block[stolen]",
                       extra=_fields(case_id=9110, params={"reason": "stolen"}, stand="qa-1",
                                     start=1020))
    final = builder.add_failure(
        "block-lost", cause=None, name=name, full_name=f"{module}#{name}", message=message,
        step="Block number", extra=_fields(case_id=9110, params=params, stand="qa-1",
                                           start=1060))
    builder.expect_attempts(final, [(by_case, True)])

    return builder.build("retries_more")


# --- sparse_data ------------------------------------------------------------------------


def sparse_data() -> Case:
    """Падения почти без данных рядом с muted, hidden и passed.

    Два падения без сообщения, трейса и лога; два — только с шагом (разные шаги); два — только
    с логом с одной ошибкой. Muted и hidden падения в группы не входят, passed — рядом.
    """
    builder = LaunchBuilder(7110, "lease smoke #7110")
    builder.add_failure("empty-archive", cause=None, name="test_archive_old_leases",
                        full_name="tests.leases.test_archive#test_archive_old_leases")
    builder.add_failure("empty-return", cause=None, name="Lease return › closes the lease",
                        full_name="tests/lease-return.test.js#Lease return closes the lease",
                        status="broken")
    builder.add_failure("step-sign", cause=None, name="test_sign_lease_with_sms_code",
                        full_name="tests.leases.test_signing#test_sign_lease_with_sms_code",
                        step="Sign lease with SMS code")
    builder.add_failure("step-passport", cause=None, name="test_upload_passport_scan",
                        full_name="tests.leases.test_documents#test_upload_passport_scan",
                        step="Upload passport scan", status="broken")
    for index, test in enumerate(("test_lease_pdf_download", "test_lease_history_download")):
        log = (
            f"[2026-10-04 11:00:0{index} +0000] [9] [INFO] Booting worker with pid: "
            f"{23 + index}\n"
            f"[2026-10-04 11:00:0{index + 2} +0000] [9] [CRITICAL] WORKER TIMEOUT "
            f"(pid:{23 + index})\n"
            f"[2026-10-04 11:00:0{index + 3} +0000] [9] [ERROR] Worker (pid:{23 + index}) was "
            "sent SIGKILL! Perhaps out of memory?\n"
        )
        builder.add_failure(
            "log-only-sigkill", cause="lease-api-worker-timeout", category="окружение",
            name=test, full_name=f"tests.leases.test_documents#{test}",
            log=log, log_name="lease-api.log",
            evidence=["was sent SIGKILL! Perhaps out of memory?"])
    builder.add_result(name="test_lease_reminder_schedule", status="failed", muted=True,
                       full_name="tests.leases.test_reminders#test_lease_reminder_schedule",
                       message="Failed: reminder for lease L-77 scheduled on 2026-10-05, "
                               "expected 2026-10-04")
    builder.add_result(name="test_lease_deposit_refund", status="broken", hidden=True,
                       full_name="tests.leases.test_deposit#test_lease_deposit_refund",
                       message="Failed: deposit for lease L-81 was not returned")
    for test in ("test_lease_create", "test_lease_extend", "test_lease_terminate"):
        builder.add_result(name=test, status="passed",
                           full_name=f"tests.leases.test_lifecycle#{test}")
    return builder.build("sparse_data")


# ---------------------------------------------------------------------------
# Бывший holdout v3 (перенесён в dev 2026-10-27, журнал — holdout_journal.md)
# ---------------------------------------------------------------------------

_DAY = 1_791_936_000  # 2026-10-14 00:00:00 UTC
_ESC = "\x1b"


def _hex(seed: str, size: int = 32) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:size]


def _hms(sec: int) -> str:
    hours, rest = divmod(sec, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _jl(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _at(hours: int, minutes: int, seconds: int = 0) -> int:
    return hours * 3600 + minutes * 60 + seconds


# --------------------------------------------------------------------------------------------
# log_formats_v3: причина видна только в логе, по группе на формат


def _httpd_log(sec: int, port: int) -> str:
    stamp = f"Wed Oct 14 {_hms(sec)}"
    return (
        "[Wed Oct 14 09:12:04.118204 2026] [mpm_event:notice] [pid 1:tid 140245] AH00489: "
        "Apache/2.4.62 (Unix) configured -- resuming normal operations\n"
        "[Wed Oct 14 09:12:04.118377 2026] [core:notice] [pid 1:tid 140245] AH00094: "
        "Command line: 'httpd -D FOREGROUND'\n"
        f"[{stamp}.402551 2026] [proxy:error] [pid 9:tid 140101] (111)Connection refused: "
        "AH00957: http: attempt to connect to 10.44.3.21:8090 (rates-backend.uat.internal:8090) "
        "failed\n"
        f"[{stamp}.402613 2026] [proxy_http:error] [pid 9:tid 140101] [client 10.44.1.7:{port}] "
        "AH01114: HTTP: failed to make connection to backend: rates-backend.uat.internal\n"
    )


def _mongo_log(sec: int, conn: int, account: str) -> str:
    def stamp(ms: int) -> dict[str, str]:
        return {"$date": f"2026-10-14T{_hms(sec)}.{ms:03d}+00:00"}

    return "".join([
        _jl({"t": stamp(118), "s": "I", "c": "NETWORK", "id": 22943, "ctx": "listener",
             "msg": "Connection accepted",
             "attr": {"remote": "10.44.6.12:40112", "connectionId": conn,
                      "connectionCount": 37}}),
        _jl({"t": stamp(121), "s": "I", "c": "NETWORK", "id": 51800, "ctx": f"conn{conn}",
             "msg": "client metadata",
             "attr": {"remote": "10.44.6.12:40112", "client": f"conn{conn}",
                      "doc": {"driver": {"name": "mongo-scala-driver", "version": "5.1.4"}}}}),
        _jl({"t": stamp(388), "s": "E", "c": "QUERY", "id": 4625501, "ctx": f"conn{conn}",
             "msg": "Plan executor error during find command",
             "attr": {"error": {"code": 292,
                                "codeName": "QueryExceededMemoryLimitNoDiskUseAllowed",
                                "errmsg": "Executor error during find command :: caused by :: "
                                          "Sort exceeded memory limit of 104857600 bytes, but "
                                          "did not opt in to external sorting."},
                      "stats": {"stage": "SORT", "nReturned": 0,
                                "totalDataSizeSorted": 104858012},
                      "cmd": {"find": "operations", "filter": {"accountId": account},
                              "sort": {"bookedAt": -1}, "$db": "statements"}}}),
        _jl({"t": stamp(392), "s": "I", "c": "NETWORK", "id": 22944, "ctx": f"conn{conn}",
             "msg": "Connection ended",
             "attr": {"remote": "10.44.6.12:40112", "connectionId": conn,
                      "connectionCount": 36}}),
    ])


def _redis_log(sec: int) -> str:
    return (
        "1:C 14 Oct 2026 09:00:00.512 # oO0OoO0OoO0Oo Redis is starting oO0OoO0OoO0Oo\n"
        "1:C 14 Oct 2026 09:00:00.512 # Redis version=7.2.5, bits=64, commit=00000000, "
        "modified=0, pid=1, just started\n"
        "1:M 14 Oct 2026 09:00:00.514 * Ready to accept connections tcp\n"
        f"1:M 14 Oct 2026 {_hms(sec)}.003 * 10000 changes in 60 seconds. Saving...\n"
        f"1:M 14 Oct 2026 {_hms(sec)}.004 # Can't save in background: fork: Cannot allocate "
        "memory\n"
        f"1:M 14 Oct 2026 {_hms(sec + 6)}.010 * 10000 changes in 60 seconds. Saving...\n"
        f"1:M 14 Oct 2026 {_hms(sec + 6)}.011 # Can't save in background: fork: Cannot "
        "allocate memory\n"
    )


def _cassandra_log(sec: int, account: str, token: str) -> str:
    query = f"SELECT * FROM ledger.entries WHERE account_id = {account} LIMIT 5000"
    return (
        "INFO  [main] 2026-10-14 08:55:12,004 StorageService.java:2943 - Node /10.44.7.3:7000 "
        "state jump to NORMAL\n"
        "INFO  [main] 2026-10-14 08:55:12,310 CassandraDaemon.java:782 - Startup complete\n"
        f"WARN  [ReadStage-2] 2026-10-14 {_hms(sec)},882 ReadCommand.java:601 - Read 1200 live "
        f"rows and 98422 tombstone cells for query {query}; token {token} "
        "(see tombstone_warn_threshold)\n"
        f"ERROR [ReadStage-3] 2026-10-14 {_hms(sec + 1)},104 StorageProxy.java:2104 - Scanned "
        f"over 100001 tombstones during query '{query}' (last scanned row token was {token} "
        f"and partion key was (({account}))); query aborted\n"
    )


def _clickhouse_log(sec: int, query_id: str, day: str) -> str:
    return (
        "2026.10.14 08:30:00.115201 [ 612 ] {} <Information> Application: Ready for "
        "connections.\n"
        f"2026.10.14 {_hms(sec)}.774012 [ 734 ] {{{query_id}}} <Error> executeQuery: Code: 60. "
        "DB::Exception: Table analytics.sessions_daily does not exist. (UNKNOWN_TABLE) "
        "(version 24.8.4.13 (official build)) (from 10.44.9.17:51920) (in query: SELECT day, "
        f"count() FROM analytics.sessions_daily WHERE day = '{day}' GROUP BY day), Stack trace "
        "(when copying this message, always include the lines below):\n"
        "\n"
        "0. DB::Exception::Exception(DB::Exception::MessageMasked&&, int, bool) @ "
        "0x000000000d2f1a3b\n"
        "1. DB::IDatabase::getTable(String const&, std::shared_ptr<DB::Context const>) const @ "
        "0x0000000011a7c2e4\n"
    )


def _airflow_log(dag: str, task: str) -> str:
    run = "scheduled__2026-10-13T07:00:00+00:00"
    site = "/home/airflow/.local/lib/python3.11/site-packages/airflow"
    return (
        "[2026-10-14, 07:00:01 UTC] {local_task_job_runner.py:123} INFO - ::group::Pre task "
        "execution logs\n"
        "[2026-10-14, 07:00:01 UTC] {taskinstance.py:2612} INFO - Dependencies all met for "
        f"dep_context=requeueable deps ti=<TaskInstance: {dag}.{task} {run} [queued]>\n"
        "[2026-10-14, 07:00:02 UTC] {taskinstance.py:2865} INFO - Starting attempt 1 of 1\n"
        "[2026-10-14, 07:00:03 UTC] {taskinstance.py:3311} ERROR - Task failed with exception\n"
        "Traceback (most recent call last):\n"
        f'  File "{site}/models/taskinstance.py", line 767, in _execute_task\n'
        "    result = _execute_callable(context=context, **execute_callable_kwargs)\n"
        f'  File "{site}/hooks/base.py", line 83, in get_connection\n'
        "    conn = Connection.get_connection_from_secrets(conn_id)\n"
        f'  File "{site}/models/connection.py", line 519, in get_connection_from_secrets\n'
        "    raise AirflowNotFoundException(f\"The conn_id `{conn_id}` isn't defined\")\n"
        "airflow.exceptions.AirflowNotFoundException: The conn_id `dwh_postgres` isn't "
        "defined\n"
        "[2026-10-14, 07:00:03 UTC] {taskinstance.py:1225} INFO - Marking task as FAILED. "
        f"dag_id={dag}, task_id={task}, run_id={run}\n"
    )


def _php_log(minute: int) -> str:
    return (
        f"[14-Oct-2026 10:{minute:02d}:12 UTC] PHP Warning:  Undefined array key \"vat_rate\" "
        "in /var/www/app/src/Report/TurnoverRow.php on line 41\n"
        f"[14-Oct-2026 10:{minute:02d}:13 UTC] PHP Fatal error:  Allowed memory size of "
        "134217728 bytes exhausted (tried to allocate 20480 bytes) in "
        "/var/www/app/vendor/phpoffice/phpspreadsheet/src/PhpSpreadsheet/Cell/Cell.php on "
        "line 214\n"
    )


def _mq_log(sec: int, pid: str) -> str:
    hours, rest = divmod(sec, 3600)
    clock = f"{hours % 12 or 12:02d}:{rest // 60:02d}:{rest % 60:02d}"
    return (
        f"10/14/2026 {clock} AM - Process({pid}) User(mqm) Program(amqrmppa)\n"
        "                    Host(mq-clr-01) Installation(Installation1)\n"
        "                    VRMF(9.3.0.15) QMgr(QM.CLR.UAT)\n"
        f"                    Time(2026-10-14T{_hms(sec)}.611Z)\n"
        "                    RemoteHost(10.52.7.14)\n"
        "                    CommentInsert1(PAYHUB.SVRCONN)\n"
        "                    CommentInsert2(svc_payhub)\n"
        "                    CommentInsert3(10.52.7.14)\n"
        "\n"
        "AMQ9777E: Channel was blocked\n"
        "\n"
        "EXPLANATION:\n"
        "The inbound channel 'PAYHUB.SVRCONN' was blocked from address '10.52.7.14' because\n"
        "the active values of the channel matched a record configured with USERSRC(NOACCESS).\n"
        "The active values of the channel were 'CLNTUSER(svc_payhub) ADDRESS(10.52.7.14)'.\n"
        "ACTION:\n"
        "Contact the systems administrator, who should examine the channel authentication\n"
        "records to ensure that the correct settings have been configured.\n"
        "----- cmcxmsgs.c : 1417 -------------------------------------------------------\n"
    )


def log_formats_v3() -> Case:
    b = LaunchBuilder(8101, "ift nightly regress #8101")

    for index, client in enumerate(["corporate", "retail"]):
        b.add_failure(
            "httpd-backend-refused", cause="rates-backend-refused", category="окружение",
            name=f"Rates for {client} clients",
            full_name=f"FX Rates API / {client} / Status code is 200",
            message="expected response to have status code 200 but got 503",
            step=f"GET /api/v1/rates/{client}",
            log=_httpd_log(_at(10, 3, 17 + index * 9), 52144 + index * 46),
            log_name="httpd-error.log",
            evidence=["AH01114: HTTP: failed to make connection to backend: "
                      "rates-backend.uat.internal"])

    for index, (account, count) in enumerate([("40817810500000004411", 214),
                                              ("40817810500000004629", 87)]):
        b.add_failure(
            "mongo-sort-limit", cause="statement-sort-memory", category="приложение",
            name=f"Statement for 90 days lists all operations of {account[-4:]}",
            full_name="ru.finplat.statements.e2e.StatementSpec",
            message=f"0 did not equal {count}",
            trace=java_trace(
                f"org.scalatest.exceptions.TestFailedException: 0 did not equal {count}",
                ["org.scalatest.matchers.MatchersHelper$.indicateFailure(MatchersHelper.scala:392)",
                 "org.scalatest.matchers.should.Matchers$AnyShouldWrapper.shouldEqual"
                 "(Matchers.scala:6919)",
                 "ru.finplat.statements.e2e.StatementSpec.$anonfun$new$3"
                 "(StatementSpec.scala:44)"]),
            step="GET /api/v2/statements?period=P90D",
            log=_mongo_log(_at(10, 5, 41 + index * 12), 4418 + index * 3, account),
            log_name="mongod.log",
            evidence=["Sort exceeded memory limit of 104857600 bytes, but did not opt in to "
                      "external sorting."])

    for index, name in enumerate(["refresh extends session", "refresh rotates token"]):
        b.add_failure(
            "redis-bgsave-fork", cause="redis-bgsave-fork-oom", category="окружение",
            name=f"test {name} (Sessions.ApiTest)", full_name=f"Sessions.ApiTest.test {name}",
            message="Assertion with == failed\ncode:  assert conn.status == 200\n"
                    "left:  500\nright: 200",
            trace=f"test/sessions/api_test.exs:{57 + index * 14}: (test)\n",
            step="PUT /api/sessions/:id/refresh",
            log=_redis_log(_at(10, 6, 2 + index * 20)), log_name="redis.log",
            evidence=["Can't save in background: fork: Cannot allocate memory"])

    for index, (account, token) in enumerate([("40817810900000000731", "-8817294715"),
                                              ("40817810900000000958", "-1127730048")]):
        url = (f"http://ledger-query.uat.internal:8080/api/v2/accounts/{account}/entries"
               "?from=2026-01-01")
        b.add_failure(
            "cassandra-tombstones", cause="ledger-tombstones", category="приложение",
            name=f"Ledger entries for account {account[-4:]}",
            full_name="classpath:ledger/entries.feature",
            message=f"status code was: 500, expected: 200, response time in milliseconds: "
                    f"{1270 + index * 33}, url: {url}, response: \n"
                    f'{{"status":500,"error":"Internal Server Error",'
                    f'"path":"/api/v2/accounts/{account}/entries"}}',
            trace="classpath:ledger/entries.feature:18\n",
            step="status 200",
            log=_cassandra_log(_at(10, 7, 15 + index * 21), account, token),
            log_name="cassandra-system.log",
            evidence=["Scanned over 100001 tombstones during query"])

    for index, day in enumerate(["2026-10-13", "2026-10-12"]):
        text = f"0 != 14 : rows in daily sessions report for {day}"
        b.add_failure(
            "clickhouse-unknown-table", cause="clickhouse-table-missing", category="приложение",
            name=f"test_daily_sessions_report_{day.replace('-', '_')}",
            full_name="tests.test_reports.DailyReportsTest",
            message=f"AssertionError: {text}",
            trace="Traceback (most recent call last):\n"
                  '  File "/builds/analytics/e2e/tests/test_reports.py", line 48, in '
                  "test_daily_sessions_report\n"
                  f'    self.assertEqual(len(rows), 14, "rows in daily sessions report for '
                  f'{day}")\n'
                  f"AssertionError: {text}\n",
            log=_clickhouse_log(_at(10, 8, 41 + index * 17), _hex(f"ch{index}", 36), day),
            log_name="clickhouse-server.err.log",
            evidence=["Table analytics.sessions_daily does not exist. (UNKNOWN_TABLE)"])

    for dag, task in [("nightly_settlement", "load_ledger"), ("nightly_fees", "load_fees")]:
        b.add_failure(
            "airflow-conn-missing", cause="airflow-conn-missing", category="окружение",
            name=f"DAG {dag} completes", full_name=f"features/dwh.feature: DAG {dag} completes",
            message=f"Assertion Failed: DAG run {dag}__2026-10-13 finished in state 'failed', "
                    "expected 'success'",
            step=f"Then the DAG run {dag} succeeds",
            log=_airflow_log(dag, task), log_name=f"{dag}.{task}.log",
            evidence=["The conn_id `dwh_postgres` isn't defined"])

    for index, report in enumerate(["turnover", "turnover-by-branch"]):
        b.add_failure(
            "php-memory-exhausted", cause="xlsx-export-memory", category="приложение",
            name=f"Download {report} report", full_name=f"ReportsCest:download{index}",
            message="Expected HTTP Status Code: 200 (OK). Actual Status Code: 500 "
                    "(Internal Server Error)",
            step=f"I send GET \"/api/v1/reports/{report}.xlsx\"",
            log=_php_log(9 + index), log_name="php-fpm-error.log",
            evidence=["Allowed memory size of 134217728 bytes exhausted"])

    for index, order in enumerate(["PO-2026-55102", "PO-2026-55117"]):
        b.add_failure(
            "mq-channel-blocked", cause="mq-chlauth-block", category="окружение",
            name=f"PaymentIsSettledAfterWindow ({order})",
            full_name="Lumen.Clearing.Tests.ClearingFlowTests.PaymentIsSettledAfterWindow",
            message="Assert.AreEqual failed. Expected:<Settled>. Actual:<Pending>. "
                    f"Payment {order} state after clearing window",
            trace="   at Lumen.Clearing.Tests.ClearingFlowTests.PaymentIsSettledAfterWindow() "
                  f"in /src/tests/ClearingFlowTests.cs:line {88 + index}\n",
            log=_mq_log(_at(10, 10, 5 + index * 40), f"3311.{27 + index}"),
            log_name="AMQERR01.LOG",
            evidence=["AMQ9777E: Channel was blocked"])

    return b.build("log_formats_v3")


# --------------------------------------------------------------------------------------------
# log_traps_v3: настоящая ошибка рядом с похожими на ошибку строками


def _quarkus_limits_log(sec: int, client: int, thread: int, error_id: str) -> str:
    def at(offset: int, ms: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{ms:03d}"

    exec_thread = f"executor-thread-{thread}"
    return (
        f"{at(-3, 14)} INFO  [io.quarkus] (main) limits-svc 3.4.1 on JVM (powered by Quarkus "
        "3.15.1) started in 2.114s. Listening on: http://0.0.0.0:8080\n"
        f"{at(-3, 15)} INFO  [io.quarkus] (main) Installed features: [agroal, cdi, "
        "hibernate-orm, jdbc-postgresql, rest, smallrye-health]\n"
        f"{at(0, 101)} INFO  [io.qua.htt.access-log] (executor-thread-1) 10.44.2.8 - - "
        '"GET /api/v1/error-catalog?lang=ru HTTP/1.1" 200 1532\n'
        f"{at(0, 233)} INFO  [com.fin.lim.LimitCheckResource] ({exec_thread}) check started "
        f"client={client} errorCount=0 lastError=none\n"
        f"{at(0, 240)} WARN  [com.fin.lim.RatesClient] ({exec_thread}) rates cache refresh "
        "failed, using previous snapshot: jakarta.ws.rs.ProcessingException: RESTEASY004655: "
        "Unable to invoke request\n"
        "\tat org.jboss.resteasy.client.jaxrs.engines.ApacheHttpClient43Engine.invoke"
        "(ApacheHttpClient43Engine.java:282)\n"
        "\tat com.finplat.limits.RatesClient.refresh(RatesClient.java:57)\n"
        f"{at(0, 251)} DEBUG [com.fin.lim.ClientRepository] ({exec_thread}) handled exception "
        "while reading optional segment: jakarta.persistence.NoResultException: No entity "
        "found for query\n"
        f"{at(0, 262)} ERROR [io.qua.ver.htt.run.QuarkusErrorHandler] ({exec_thread}) HTTP "
        f"Request to /api/v1/limits/check failed, error id: {error_id}-1: "
        "org.hibernate.exception.SQLGrammarException: JDBC exception executing SQL [select "
        "lr1_0.id,lr1_0.segment,lr1_0.max_amount from limit_rules_v2 lr1_0 where "
        'lr1_0.segment=?] [ERROR: relation "limit_rules_v2" does not exist\n'
        "  Position: 71] [n/a]\n"
        "\tat org.hibernate.exception.internal.SQLStateConversionDelegate.convert"
        "(SQLStateConversionDelegate.java:91)\n"
        "\tat com.finplat.limits.LimitRuleRepository.findBySegment"
        "(LimitRuleRepository.java:38)\n"
        f"{at(1, 5)} INFO  [io.qua.htt.access-log] (executor-thread-1) 10.44.2.8 - - "
        '"GET /q/health/ready HTTP/1.1" 200 46\n'
    )


def _uvicorn_log(member: int, port: int) -> str:
    def lvl(color: str, name: str, pad: str) -> str:
        return f"{_ESC}[{color}m{name}{_ESC}[0m:{pad}"

    info = lvl("32", "INFO", "     ")
    return (
        f"{info}Started server process [1]\n"
        f"{info}Waiting for application startup.\n"
        f"{info}Application startup complete.\n"
        f"{info}Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)\n"
        f'{info}10.44.3.15:{port - 1} - "GET /v1/errors/recent?limit=20 HTTP/1.1" '
        f"{_ESC}[32m200 OK{_ESC}[0m\n"
        f"{lvl('33', 'WARNING', '  ')}Invalid HTTP request received.\n"
        f'{info}10.44.3.15:{port} - "GET /v1/members/{member}/tier HTTP/1.1" '
        f"{_ESC}[91m500 Internal Server Error{_ESC}[0m\n"
        f"{lvl('31', 'ERROR', '    ')}Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        '  File "/app/.venv/lib/python3.12/site-packages/uvicorn/protocols/http/'
        'httptools_impl.py", line 409, in run_asgi\n'
        "    result = await app(  # type: ignore[func-returns-value]\n"
        '  File "/app/loyalty/api/members.py", line 88, in get_tier\n'
        "    async with db.acquire() as conn:\n"
        '  File "/app/.venv/lib/python3.12/site-packages/asyncpg/pool.py", line 1024, in '
        "__aenter__\n"
        "    self.connection = await self.pool._acquire(self.timeout)\n"
        '  File "/app/.venv/lib/python3.12/site-packages/asyncpg/connect_utils.py", line 1102, '
        "in _connect_addr\n"
        "    return await __connect_addr(params, True, *args)\n"
        "asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients already\n"
        f'{info}10.44.3.15:{port + 1} - "GET /v1/members/{member}/errors-history HTTP/1.1" '
        f"{_ESC}[32m200 OK{_ESC}[0m\n"
    )


def _log4net_fx_log(sec: int, deal: str, pair: str, age: int) -> str:
    def at(offset: int, ms: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{ms:03d}"

    lines = [
        f"{at(-5, 101)} [1] INFO  Treasury.Host.Program [(null)] - Treasury.FxDesk 5.2.0 "
        "started, environment=UAT",
        f"{at(0, 330)} [14] INFO  Treasury.Fx.ErrorBudget [(null)] - error budget report: "
        "errors=0, warnings=2",
        f"{at(0, 512)} [14] WARN  Treasury.Fx.QuoteCache [(null)] - quote cache refresh failed, "
        "keeping previous snapshot",
        "System.Security.Authentication.AuthenticationException: Authentication failed because "
        "the remote party sent a TLS alert: 'HandshakeFailure'.",
        "   at System.Net.Security.SslStream.ForceAuthenticationAsync[TIOAdapter](Boolean "
        "receiveFirst, Byte[] reAuthenticationData, CancellationToken cancellationToken)",
        "   at Treasury.Fx.QuoteCache.RefreshAsync() in /src/Treasury.Fx/QuoteCache.cs:line 61",
        f"{at(0, 601)} [14] DEBUG Treasury.Fx.DealBooking [deal={deal}] - validating deal, "
        "errors: none",
        f"{at(0, 734)} [14] ERROR Treasury.Fx.DealBooking [deal={deal}] - Deal rejected: "
        f"quote {pair} is stale by {age} s (limit 60 s), source=MOEX-UAT",
        f"{at(1, 2)} [9] INFO  Treasury.Fx.Health [(null)] - /health/errors responded 200",
    ]
    return "\r\n".join(lines) + "\r\n"


def _sidekiq_noise(start: int, count: int, offset: int) -> str:
    classes = ("BalanceSnapshotJob", "CardLimitSyncJob", "RateAlertJob", "AuditFlushJob")
    lines = []
    for index in range(count):
        number = offset + index
        stamp = f"2026-10-14T{_hms(start + index // 4)}.{(number * 137) % 1000:03d}Z"
        head = (f"{stamp} pid=7 tid={(number % 9) + 2}k{number % 7} "
                f"class={classes[number % 4]} jid={_hex(f'jid{number}', 24)}")
        lines.append(f"{head} INFO: start\n")
        lines.append(f"{head} elapsed=0.{(number * 53) % 1000:03d} INFO: done\n")
    return "".join(lines)


def _sidekiq_statement_log(account: str, jid: str, part: int) -> str:
    error_at = f"2026-10-14T10:31:{12 + part:02d}"
    head = f"pid=7 tid=3c{part} class=StatementPdfJob jid={jid}"
    return (
        "2026-10-14T10:25:00.004Z pid=7 tid=2kq INFO: Booted Rails 7.2.1 application in "
        "production environment\n"
        "2026-10-14T10:25:00.006Z pid=7 tid=2kq INFO: Running in ruby 3.3.5 +YJIT\n"
        + _sidekiq_noise(_at(10, 25, 1), 800, part * 5000)
        + f"{error_at}.401Z {head} INFO: start\n"
        f"{error_at}.733Z {head} elapsed=0.332 INFO: fail\n"
        f'{error_at}.734Z pid=7 tid=3c{part} WARN: {{"context":"Job raised exception",'
        f'"job":{{"retry":3,"queue":"pdf","class":"StatementPdfJob","args":["{account}"],'
        f'"jid":"{jid}"}}}}\n'
        f"{error_at}.734Z pid=7 tid=3c{part} WARN: Prawn::Errors::IncompatibleStringEncoding: "
        "Your document includes text that's not compatible with the Windows-1252 character "
        "set.\n"
        f"{error_at}.734Z pid=7 tid=3c{part} WARN: /app/app/pdfs/statement_pdf.rb:57:in "
        "'StatementPdf#render_header'\n"
        + _sidekiq_noise(_at(10, 31, 14), 800, part * 5000 + 800)
    )


def _statement_page_attachments(account: str, request_id: str) -> list[tuple[str, str, str]]:
    request = json.dumps({"url": f"https://ib.uat.internal/api/statements?account={account}",
                          "method": "GET", "headers": {"X-Error-Policy": "strict"},
                          "errorPolicy": "strict"}, indent=2)
    console = (
        "https://ib.uat.internal/statements 0:0 Failed to load resource: the server responded "
        "with a status of 500 ()\n"
        "https://ib.uat.internal/assets/index-3c9a71.js 2:18044 [statements] retry 1/1 "
        "after error\n"
    )
    server = "".join([
        _jl({"@t": "2026-10-14T10:33:01.2210000Z", "@mt": "Request starting {Protocol} "
             "{Method} {Path}", "Protocol": "HTTP/1.1", "Method": "GET",
             "Path": "/api/statements", "RequestId": request_id}),
        _jl({"@t": "2026-10-14T10:33:01.2290000Z", "@mt": "Error budget check passed, "
             "{ErrorCount} errors in window", "ErrorCount": 0, "RequestId": request_id}),
        _jl({"@t": "2026-10-14T10:33:01.9020000Z", "@mt": "Statement query failed for account "
             "{AccountId}", "@l": "Error",
             "@x": "Oracle.ManagedDataAccess.Client.OracleException (0x80004005): ORA-01652: "
                   "unable to extend temp segment by 128 in tablespace TEMP\n"
                   "   at OracleInternal.ServiceObjects.OracleConnectionImpl.VerifyExecution("
                   "Int32& cursorId, Boolean bThrowArrayBindRelatedErrors)\n"
                   "   at Statements.Data.StatementRepository.QueryAsync(String accountId) in "
                   "/src/Statements.Data/StatementRepository.cs:line 74",
             "AccountId": account, "SourceContext": "Statements.Data.StatementRepository",
             "RequestId": request_id}),
    ])
    return [("request.json", request, "application/json"),
            ("browser-console.log", console, "text/plain"),
            ("statements-api.clef", server, "text/plain")]


def _env_logger_log(sec: int, batch: int, day: str) -> str:
    def at(offset: int) -> str:
        return f"2026-10-14T{_hms(sec + offset)}Z"

    host = "files.uat.internal"
    return (
        f"[{at(0)} INFO  ledger_sync] starting ledger-sync 0.14.2, "
        "error_dir=/var/lib/ledger-sync/errors\n"
        f"[{at(1)} DEBUG hyper_util::client::legacy::pool] reuse idle connection for "
        f'("https", {host})\n'
        f"[{at(1)} INFO  ledger_sync::http] GET https://{host}/api/errors?since={day} -> "
        "200 OK\n"
        f"[{at(2)} DEBUG ledger_sync::fetch] handled error, will retry: error sending request "
        f"for url (https://{host}/ledger/{day}.csv): connection closed before message "
        "completed\n"
        f"[{at(2)} WARN  ledger_sync::fetch] attempt 1/3 failed: error decoding response "
        "body: unexpected EOF during chunk size line; retrying in 500ms\n"
        f"[{at(3)} INFO  ledger_sync::fetch] downloaded ledger-{day}.csv (48211 rows)\n"
        f"[{at(3)} ERROR ledger_sync::worker] batch {batch} rejected: checksum mismatch for "
        f"ledger-{day}.csv (expected sha256 9f1c2e04, got 3b7a90d1)\n"
        f"[{at(4)} INFO  ledger_sync] errors=1 warnings=1, next run in 300s\n"
    )


def log_traps_v3() -> Case:
    b = LaunchBuilder(8102, "uat regress #8102")

    for index, client in enumerate([7781, 7790]):
        b.add_failure(
            "limits-relation-missing", cause="limits-migration-missing", category="приложение",
            name=f"limit check for client segment {index + 1}",
            full_name="tests/limits/check.spec.ts > limits > check",
            message="AssertionError: expected 500 to be 200 // Object.is equality",
            trace="AssertionError: expected 500 to be 200 // Object.is equality\n"
                  f"    at /builds/limits/api-tests/tests/limits/check.spec.ts:{31 + index}:30\n",
            step="POST /api/v1/limits/check",
            log=_quarkus_limits_log(_at(10, 20, 3 + index * 30), client, 2 + index,
                                    _hex(f"q{index}", 8) + "-77d0-4b8e-9b31-2f6a0c1d5e93"),
            log_name="limits-svc.log",
            evidence=['relation "limit_rules_v2" does not exist'])

    for index, member in enumerate([77120, 77131]):
        b.add_failure(
            "asyncpg-too-many-clients", cause="loyalty-db-connections", category="окружение",
            name=f"Member {member} Tier Is Returned", full_name="Loyalty.Members",
            message=f"Url: http://loyalty.uat.internal:8000/v1/members/{member}/tier "
                    "Expected status: 500 != 200",
            step="GET On Session",
            log=_uvicorn_log(member, 40212 + index * 7), log_name="loyalty-api.log",
            evidence=["asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients "
                      "already"])

    for index, (deal, pair, age) in enumerate([("FX-88120", "EUR/RUB", 914),
                                               ("FX-88134", "USD/RUB", 921)]):
        b.add_failure(
            "fx-quote-stale", cause="fx-quote-stale", category="окружение",
            name=f"BookDealAtMarketRate ({pair})",
            full_name="Treasury.E2E.FxDeskTests.BookDealAtMarketRate",
            message="Assert.AreEqual failed. Expected:<Booked>. Actual:<Rejected>. FX deal "
                    "status",
            trace="   at Treasury.E2E.FxDeskTests.BookDealAtMarketRate(String pair) in "
                  "/src/Treasury.E2E/FxDeskTests.cs:line 47\n",
            step="Book FX deal",
            log=_log4net_fx_log(_at(10, 22, 5 + index * 25), deal, pair, age),
            log_name="fxdesk.log",
            evidence=[f"Deal rejected: quote {pair} is stale"])

    for index, account in enumerate(["40817810100000003301", "40817810100000003318"]):
        b.add_failure(
            "statement-pdf-encoding", cause="pdf-font-encoding", category="приложение",
            name=f"test_statement_pdf_is_ready_{index + 1}",
            full_name="StatementPdfTest#test_statement_pdf_is_ready",
            message=f"Statement PDF for account {account} should be ready.\n"
                    'Expected: "ready"\n  Actual: "failed"',
            trace=f"test/integration/statement_pdf_test.rb:{24 + index}:in "
                  "'StatementPdfTest#test_statement_pdf_is_ready'\n",
            step="Request account statement PDF",
            log=_sidekiq_statement_log(account, _hex(f"pdf{index}", 24), index),
            log_name="sidekiq.log",
            evidence=["Prawn::Errors::IncompatibleStringEncoding: Your document includes text "
                      "that's not compatible with the Windows-1252 character set."])

    for index, account in enumerate(["40817810300000002291", "40817810300000002305"]):
        b.add_failure(
            "oracle-temp-tablespace", cause="oracle-temp-tablespace", category="окружение",
            name=f"Statement rows are shown ({index + 1})",
            full_name="Statements / Statement rows are shown",
            message="The specified selector does not match any element in the DOM tree.\n\n"
                    " > | Selector('[data-testid=\"statement-row\"]')",
            step="Open statement page",
            logs=_statement_page_attachments(account, f"0HN7A1B2C3D4E:0000000{index + 2}"),
            evidence=["ORA-01652: unable to extend temp segment by 128 in tablespace TEMP"])

    for index, (batch, day) in enumerate([(4471, "2026-10-13"), (4472, "2026-10-12")]):
        message = (f"thread 'sync::imports_daily_ledger_{index + 1}' panicked at "
                   f"tests/sync.rs:{74 + index * 9}:9:\n"
                   "assertion `left == right` failed: ledger import status\n"
                   '  left: "Rejected"\n right: "Imported"')
        b.add_failure(
            "ledger-checksum", cause="ledger-checksum-mismatch", category="данные",
            name=f"sync::imports_daily_ledger_{index + 1}", full_name="ledger_sync::tests::sync",
            message=message,
            log=_env_logger_log(_at(10, 35 + index * 5), batch, day), log_name="ledger-sync.log",
            evidence=[f"checksum mismatch for ledger-{day}.csv"])

    return b.build("log_traps_v3")


# --------------------------------------------------------------------------------------------
# same_symptom_causes_v3: одинаковые сообщение и шаг, причины только в логах


def _wls(sec: int, severity: str, subsystem: str, thread: str, msg_id: str, text: str) -> str:
    hours, rest = divmod(sec, 3600)
    ms = (sec * 37) % 1000
    stamp = (f"Oct 14, 2026 {hours % 12 or 12}:{rest // 60:02d}:{rest % 60:02d},{ms:03d} "
             f"{'AM' if hours < 12 else 'PM'} UTC")
    value = {"Error": 8, "Warning": 16, "Notice": 32, "Info": 64}[severity]
    return (f"####<{stamp}> <{severity}> <{subsystem}> <wls-trn-01> <TransfersServer1> "
            f"<{thread}> <<WLS Kernel>> <> <{_hex(f'ecid{sec}', 8)}-5c1e-4b7a-9f02-"
            f"{_hex(f'ecid{sec}', 12)}-0000012a> <{(_DAY + sec) * 1000 + ms}> "
            f"<[severity-value: {value}] [rid: 0] [partition-id: 0] [partition-name: DOMAIN] > "
            f"<{msg_id}> <{text}>\n")


def _wls_thread(number: int) -> str:
    return (f"[ACTIVE] ExecuteThread: '{number}' for queue: 'weblogic.kernel.Default "
            "(self-tuning)'")


def _transfers_log(kind: str, sec: int, transfer: str, thread: int) -> str:
    lines = [
        _wls(sec - 600, "Notice", "WebLogicServer", "main", "BEA-000360",
             "The server started in RUNNING mode."),
        _wls(sec - 30, "Info", "Health", _wls_thread(3), "BEA-310002",
             "61% of the total memory in the server is free."),
        _wls(sec, "Info", "TransfersApp", _wls_thread(thread), "BEA-000000",
             f"TransferResource: create transfer {transfer} accepted"),
    ]
    if kind == "db":
        lines += [
            _wls(sec + 2, "Error", "JDBC", _wls_thread(thread), "BEA-001129",
                 'Received exception while creating connection for pool "TransfersDS": IO '
                 "Error: The Network Adapter could not establish the connection "
                 f"(CONNECTION_ID={_hex(f'cid{transfer}', 22)}==)"),
            _wls(sec + 2, "Warning", "JDBC", _wls_thread(thread), "BEA-001156",
                 "Stack trace associated with message 001129 follows: \n\n"
                 "java.sql.SQLRecoverableException: IO Error: The Network Adapter could not "
                 "establish the connection\n"
                 "\tat oracle.jdbc.driver.T4CConnection.logon(T4CConnection.java:891)\n"
                 "\tat weblogic.jdbc.common.internal.ConnectionEnvFactory.makeConnection"
                 "(ConnectionEnvFactory.java:356)"),
        ]
    elif kind == "lock":
        lines += [
            _wls(sec + 1, "Warning", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"LimitService: waiting for TRANSFERS.PKG_LIMITS for transfer {transfer}"),
            _wls(sec + 31, "Error", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"LimitService: transfer {transfer} failed: ORA-04021: timeout occurred while "
                 "waiting to lock object TRANSFERS.PKG_LIMITS"),
        ]
    else:
        lines += [
            _wls(sec + 30, "Error", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"AmlScreeningClient: screening request for transfer {transfer} timed out after "
                 "30000 ms (https://aml-screen.uat.internal/api/v1/screen)"),
        ]
    lines.append(_wls(sec + 40, "Warning", "WebLogicServer", _wls_thread(1), "BEA-000337",
                      f"[STUCK] ExecuteThread: '{thread}' for queue: 'weblogic.kernel.Default "
                      "(self-tuning)' has been busy for \"612\" seconds working on the request "
                      "\"Http Request Information: weblogic.servlet.internal.ServletRequestImpl"
                      "[POST /v2/transfers]\", which is more than the configured time "
                      "(StuckThreadMaxTime) of \"600\" seconds."))
    return "".join(lines)


def _payorders_shared_log(ops: list[tuple[str, str]]) -> str:
    """Общий лог сервиса платёжных поручений: операции тестов и чужие операции."""
    lines = [
        "2026-10-14 10:44:00,010 [1] INFO  PayOrders.Api.Program [(null)] - PayOrders.Api "
        "7.1.3 started on http://+:8080",
        "2026-10-14 10:44:30,000 [7] INFO  PayOrders.Jobs.OutboxRelay [(null)] - relayed 0 "
        "messages, 0 errors",
    ]
    errors = {
        "data": 'Confirm failed: ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE" '
                "(actual: {n}, maximum: 210)",
        "key": "Confirm failed: signing service rejected request: key alias 'pay-sign-2025' "
               "expired on 2026-10-01",
        "other": "Confirm failed: payer account 40702810700000009911 is closed",
    }
    for index, (corr, kind) in enumerate(ops):
        stamp = f"2026-10-14 10:45:{index * 4 + 1:02d}"
        thread = 20 + index % 5
        lines.append(f"{stamp},{110 + index:03d} [{thread}] INFO  PayOrders.Api.ConfirmHandler "
                     f"[corr={corr}] - confirm payment order requested")
        if kind == "ok":
            lines.append(f"{stamp},{480 + index:03d} [{thread}] INFO  "
                         f"PayOrders.Api.ConfirmHandler [corr={corr}] - order confirmed")
        else:
            text = errors[kind].replace("{n}", str(212 + index))
            lines.append(f"{stamp},{480 + index:03d} [{thread}] ERROR "
                         f"PayOrders.Api.ConfirmHandler [corr={corr}] - {text}")
    lines.append("2026-10-14 10:46:00,002 [7] ERROR PayOrders.Jobs.OutboxRelay [(null)] - "
                 "broker rabbit-uat-2 unreachable, will retry in 30 s")
    return "\n".join(lines) + "\n"


def _k8s_events_oom(pod: str, pid: int) -> str:
    return (
        "LAST SEEN   TYPE      REASON       OBJECT                                MESSAGE\n"
        f"6m12s       Normal    Pulled       pod/{pod}   Container image "
        '"registry.uat.internal/statements-api:2.31.0" already present on machine\n'
        f"6m11s       Normal    Started      pod/{pod}   Started container statements-api\n"
        "2m03s       Warning   OOMKilling   node/uat-worker-07                    Memory cgroup "
        f"out of memory: Killed process {pid} (python3) total-vm:2843120kB, "
        "anon-rss:1048200kB, file-rss:10240kB, shmem-rss:0kB, UID:1000 pgtables:2412kB "
        "oom_score_adj:986\n"
        f"2m01s       Warning   BackOff      pod/{pod}   Back-off restarting failed container "
        f"statements-api in pod {pod}_statements(5f1e0c2a-9d3b-4c71-8e55-0a3b9c7d2e10)\n"
    )


def _filesgw_panic_log(port: int, goroutine: int) -> str:
    return (
        "2026/10/14 10:49:58 files-gw 1.9.0 listening on :8080\n"
        f"2026/10/14 10:50:03 http: panic serving 10.44.5.20:{port}: runtime error: slice "
        "bounds out of range [:-1]\n"
        f"goroutine {goroutine} [running]:\n"
        "net/http.(*conn).serve.func1()\n"
        "\t/usr/local/go/src/net/http/server.go:1947 +0xbe\n"
        "panic({0x9c1a20?, 0xc0003a2f30?})\n"
        "\t/usr/local/go/src/runtime/panic.go:785 +0x132\n"
        "filesgw/internal/multipart.(*Reader).boundary(...)\n"
        "\t/src/internal/multipart/reader.go:118 +0x2d4\n"
        "filesgw/internal/upload.(*Handler).ServeHTTP(0xc0001a6000, {0xb2e7c0, 0xc0004c2000}, "
        "0xc0003f8000)\n"
        "\t/src/internal/upload/handler.go:64 +0x1a5\n"
    )


def _aiohttp_trace(test: str, line: int) -> str:
    site = "/builds/statements/e2e/.venv/lib/python3.12/site-packages/aiohttp"
    return (
        "Traceback (most recent call last):\n"
        f'  File "/builds/statements/e2e/tests/test_upload.py", line {line}, in {test}\n'
        "    resp = await self.client.upload(path)\n"
        '  File "/builds/statements/e2e/support/client.py", line 44, in upload\n'
        "    async with self._session.post(url, data=form) as resp:\n"
        f'  File "{site}/client.py", line 1197, in __aenter__\n'
        "    self._resp = await self._coro\n"
        f'  File "{site}/client_reqrep.py", line 1059, in start\n'
        "    message, payload = await protocol.read()  # type: ignore[union-attr]\n"
        f'  File "{site}/streams.py", line 643, in read\n'
        "    await self._waiter\n"
        "aiohttp.client_exceptions.ServerDisconnectedError: Server disconnected\n"
    )


def same_symptom_causes_v3() -> Case:
    b = LaunchBuilder(8103, "ift regress transfers #8103")

    timeout = ('transfer_test.go:88: create transfer: Post "http://transfers.uat.internal:8080/'
               'v2/transfers": context deadline exceeded (Client.Timeout exceeded while awaiting '
               "headers)")
    transfers = [
        ("transfers-db", "transfers-db-unreachable", "db",
         ['Received exception while creating connection for pool "TransfersDS": IO Error: The '
          "Network Adapter could not establish the connection"]),
        ("transfers-db", "transfers-db-unreachable", "db", []),
        ("limits-package-lock", "limits-package-lock", "lock",
         ["ORA-04021: timeout occurred while waiting to lock object TRANSFERS.PKG_LIMITS"]),
        ("aml-timeout", "aml-screening-slow", "aml",
         ["screening request for transfer", "timed out after 30000 ms "
          "(https://aml-screen.uat.internal/api/v1/screen)"]),
        ("transfers-db", "transfers-db-unreachable", "db", []),
        ("limits-package-lock", "limits-package-lock", "lock", []),
        ("aml-timeout", "aml-screening-slow", "aml", []),
        ("limits-package-lock", "limits-package-lock", "lock", []),
    ]
    for index, (group, cause, kind, evidence) in enumerate(transfers):
        transfer = f"TR-2026-{30411 + index * 7}"
        b.add_failure(
            group, cause=cause, category="окружение",
            name=f"TestCreateTransfer/{['sbp', 'card', 'swift', 'internal'][index % 4]}_{index}",
            full_name="transfers/e2e.TestCreateTransfer",
            message=timeout, step="Create transfer",
            log=_transfers_log(kind, _at(10, 40, index * 50), transfer, 7 + index),
            log_name="TransfersServer1.log", evidence=evidence)

    corr = [_hex(f"corr{index}", 8) + "-0b6a-4c55-9a1e-" + _hex(f"corr{index}", 12)
            for index in range(7)]
    shared = _payorders_shared_log([(corr[0], "data"), (corr[1], "ok"), (corr[2], "key"),
                                    (corr[3], "other"), (corr[4], "data"), (corr[5], "key"),
                                    (corr[6], "ok")])
    for index, (op, group, cause, category, evidence) in enumerate([
        (0, "purpose-too-long", "purpose-too-long", "данные",
         'ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE"'),
        (2, "sign-key-expired", "sign-key-expired", "окружение",
         "key alias 'pay-sign-2025' expired on 2026-10-01"),
        (4, "purpose-too-long", "purpose-too-long", "данные",
         'ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE"'),
        (5, "sign-key-expired", "sign-key-expired", "окружение",
         "key alias 'pay-sign-2025' expired on 2026-10-01"),
    ]):
        response = json.dumps({
            "type": "https://tools.ietf.org/html/rfc9110#section-15.6.1",
            "title": "An error occurred while processing your request.", "status": 500,
            "correlationId": corr[op]}, indent=2)
        b.add_failure(
            group, cause=cause, category=category,
            name=f"ConfirmPaymentOrder_{['Budget', 'Tax', 'Customs', 'Salary'][index]}",
            full_name="PayOrders.E2E.ConfirmTests.ConfirmPaymentOrder",
            message="Assert.AreEqual failed. Expected:<201>. Actual:<500>. "
                    "POST /api/payment-orders",
            trace="   at PayOrders.E2E.ConfirmTests.ConfirmPaymentOrder(String kind) in "
                  "/src/PayOrders.E2E/ConfirmTests.cs:line 52\n",
            step="Confirm payment order",
            log=shared, log_name="payorders-api.log",
            logs=[("response.json", response, "application/json")],
            evidence=[evidence])

    for index, (group, cause, test) in enumerate([
        ("statements-oom", "statements-api-oom", "test_upload_mt940"),
        ("filesgw-panic", "files-gw-multipart-panic", "test_upload_camt053"),
        ("statements-oom", "statements-api-oom", "test_upload_1c_format"),
        ("filesgw-panic", "files-gw-multipart-panic", "test_upload_csv"),
    ]):
        if group == "statements-oom":
            log = _k8s_events_oom("statements-api-5c8d7f9b6-k2xwq", 31877 + index)
            log_name, evidence = "k8s-events.txt", "Memory cgroup out of memory: Killed process"
        else:
            log = _filesgw_panic_log(39122 + index, 2211 + index * 40)
            log_name, evidence = "files-gw.log", "runtime error: slice bounds out of range [:-1]"
        b.add_failure(
            group, cause=cause, category="приложение",
            name=test, full_name=f"tests.test_upload.UploadTest.{test}",
            message="aiohttp.client_exceptions.ServerDisconnectedError: Server disconnected",
            trace=_aiohttp_trace(test, 61 + index * 12), step="Upload statement file",
            log=log, log_name=log_name, evidence=[evidence])

    return b.build("same_symptom_causes_v3")


# --------------------------------------------------------------------------------------------
# hosts_locators_v3: один шаг и одна обёртка, различаются хост или локатор


def _feign_trace(host: str, path: str, test_class: str, method: str, line: int) -> str:
    return java_trace(
        f"feign.RetryableException: Connection refused executing POST http://{host}:8080{path}",
        ["feign.FeignException.errorExecuting(FeignException.java:300)",
         "feign.SynchronousMethodHandler.executeAndDecode(SynchronousMethodHandler.java:105)",
         "feign.SynchronousMethodHandler.invoke(SynchronousMethodHandler.java:53)",
         "feign.ReflectiveFeign$FeignInvocationHandler.invoke(ReflectiveFeign.java:104)",
         "jdk.proxy2/jdk.proxy2.$Proxy41.post(Unknown Source)",
         "ru.finplat.e2e.support.PartnerGateway.call(PartnerGateway.java:42)",
         f"ru.finplat.e2e.{test_class}.{method}({test_class.split('.')[-1]}.java:{line})",
         "java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke"
         "(DirectMethodHandleAccessor.java:103)",
         "org.testng.internal.invokers.MethodInvocationHelper.invokeMethod"
         "(MethodInvocationHelper.java:139)"])


def _netty_trace(host: str, address: str, spec: str, line: int) -> str:
    return java_trace(
        "io.netty.channel.AbstractChannel$AnnotatedConnectException: Connection refused: "
        f"{host}/{address}",
        ["io.netty.channel.unix.Errors.newConnectException0(Errors.java:166)",
         "io.netty.channel.unix.Errors.handleConnectErrno(Errors.java:131)",
         "io.netty.channel.epoll.AbstractEpollChannel$AbstractEpollUnsafe.finishConnect"
         "(AbstractEpollChannel.java:691)",
         "ru.finplat.e2e.support.ServiceCall.invoke(ServiceCall.kt:31)",
         f"ru.finplat.e2e.{spec}$1$1.invokeSuspend({spec.split('.')[-1]}.kt:{line})"])


def _fetch_trace(url: str, address: str, test_file: str, line: int) -> str:
    return (
        f"FetchError: request to {url} failed, reason: connect ECONNREFUSED {address}\n"
        "    at ClientRequest.<anonymous> (node_modules/node-fetch/lib/index.js:1505:11)\n"
        "    at ClientRequest.emit (node:events:519:28)\n"
        "    at apiCall (support/apiCall.js:18:9)\n"
        f"    at Context.<anonymous> (test/{test_file}:{line}:24)\n"
    )


def _cypress_get(locator: str, spec: str, line: int) -> tuple[str, str]:
    message = (f"AssertionError: Timed out retrying after 10000ms: Expected to find element: "
               f"`{locator}`, but never found it.")
    trace = (f"{message}\n"
             "    at Context.eval (webpack://ib-e2e/./cypress/support/commands.ts:14:13)\n"
             f"    at Context.eval (webpack://ib-e2e/./cypress/e2e/{spec}:{line}:8)\n")
    return message, trace


def _espresso(view_id: int, name: str, test: str, line: int) -> tuple[str, str]:
    message = ("androidx.test.espresso.NoMatchingViewException: No views in hierarchy found "
               f"matching: view.getId() is <{view_id}/com.lumen.mobile:id/{name}>")
    trace = (f"{message}\n\nView Hierarchy:\n"
             "+>DecorView{id=-1, visibility=VISIBLE, width=1080, height=2400, has-focus=false, "
             "has-focusable=true, has-window-focus=true, is-clickable=false, is-enabled=true, "
             "is-focused=false, is-focusable=false, is-layout-requested=false, "
             "is-selected=false, layout-params=LayoutParams{(0,0)(fillxfill) ty=BASE_APPLICATION"
             "}, tag=null, root-is-layout-requested=false, has-input-connection=false, x=0.0, "
             "y=0.0, child-count=3}\n"
             "\tat dalvik.system.VMStack.getThreadStackTrace(Native Method)\n"
             "\tat androidx.test.espresso.base.DefaultFailureHandler.getUserFriendlyError"
             "(DefaultFailureHandler.java:92)\n"
             "\tat androidx.test.espresso.ViewInteraction.perform(ViewInteraction.java:262)\n"
             "\tat com.lumen.mobile.e2e.support.Screen.tap(Screen.kt:22)\n"
             f"\tat com.lumen.mobile.e2e.{test}(TransferTest.kt:{line})\n")
    return message, trace


def hosts_locators_v3() -> Case:
    b = LaunchBuilder(8104, "ift e2e #8104")

    for host, cause, path, cases in [
        ("kyc-adapter.ift.local", "kyc-adapter-down", "/v1/checks",
         [("onboarding.OnboardingIT", "verifyClient", 77),
          ("onboarding.OnboardingIT", "verifyPassportData", 93)]),
        ("scoring-adapter.ift.local", "scoring-adapter-down", "/v1/scores",
         [("loans.LoanApplicationIT", "scoreApplicant", 58),
          ("loans.LoanApplicationIT", "scoreCoBorrower", 71)]),
    ]:
        for test_class, method, line in cases:
            b.add_failure(
                f"feign-{cause}", cause=cause, category="окружение",
                name=method, full_name=f"ru.finplat.e2e.{test_class}.{method}",
                message=f"Connection refused executing POST http://{host}:8080{path}",
                trace=_feign_trace(host, path, test_class, method, line),
                step="Вызвать партнёрский шлюз",
                evidence=[f"Connection refused executing POST http://{host}:8080{path}"])

    for host, address, cause, spec, line in [
        ("geo-coder.ift.local", "10.61.2.8:9000", "geo-coder-down", "delivery.AddressSpec", 27),
        ("tariff-engine.ift.local", "10.61.2.19:9100", "tariff-engine-down",
         "tariffs.TariffSpec", 40),
    ]:
        b.add_failure(
            f"vertx-{cause}", cause=cause, category="окружение",
            name=f"{spec.split('.')[-1]} resolves data", full_name=f"ru.finplat.e2e.{spec}",
            message=f"Connection refused: {host}/{address}",
            trace=_netty_trace(host, address, spec, line), step="Request downstream service",
            evidence=[f"Connection refused: {host}/{address}"])

    for url, address, cause, tests in [
        ("https://cards-issuer.ift.local/api/v3/cards", "10.61.4.12:443", "cards-issuer-down",
         [("cards/issue.test.js", "issues virtual card", 33),
          ("cards/issue.test.js", "issues plastic card", 51)]),
        ("https://push-gw.ift.local/api/v1/send", "10.61.4.30:443", "push-gw-down",
         [("push/send.test.js", "sends transaction push", 20),
          ("push/send.test.js", "sends marketing push", 38)]),
    ]:
        for test_file, title, line in tests:
            b.add_failure(
                f"fetch-{cause}", cause=cause, category="окружение",
                name=title, full_name=f"test/{test_file} {title}",
                message=f"request to {url} failed, reason: connect ECONNREFUSED {address}",
                trace=_fetch_trace(url, address, test_file, line), step="call backend",
                evidence=[f"connect ECONNREFUSED {address}"])

    for locator, tests in [
        ('[data-cy="loan-offer-card"]', [("shows personal offer", 22), ("shows top-up offer", 41)]),
        ('[data-cy="loan-calculator"]', [("recalculates payment", 63), ("changes term", 80)]),
    ]:
        for title, line in tests:
            message, trace = _cypress_get(locator, "loans/offers.cy.ts", line)
            b.add_failure(
                f"cy-{locator.split('=')[1].strip(chr(34) + ']')}", cause=None,
                name=title, full_name=f"cypress/e2e/loans/offers.cy.ts Loan offers {title}",
                message=message, trace=trace, step="Open loan offers")

    for view_id, view, test, line in [
        (2131296451, "btn_transfer_confirm", "transfers.TransferTest.confirmTransfer", 51),
        (2131296388, "card_balance_value", "transfers.TransferTest.showsBalanceAfter", 77),
    ]:
        message, trace = _espresso(view_id, view, test, line)
        b.add_failure(
            f"espresso-{view}", cause=None, name=test.split(".")[-1],
            full_name=f"com.lumen.mobile.e2e.{test}", message=message, trace=trace,
            step="Tap on view")

    return b.build("hosts_locators_v3")


# --------------------------------------------------------------------------------------------
# shared_assert_bg_v3: общий ассерт, разные ошибки, фоновые ошибки других сервисов


def _akka(sec: int, ms: int, level: str, dispatcher: int, path: str, text: str) -> str:
    hours, rest = divmod(sec, 3600)
    return (f"[{level}] [10/14/2026 {hours:02d}:{rest // 60:02d}:{rest % 60:02d}.{ms:03d}] "
            f"[cards-akka.actor.default-dispatcher-{dispatcher}] [akka://cards/{path}] {text}\n")


_BG_SHARED = (
    _akka(_at(11, 0), 512, "ERROR", 3, "user/audit-publisher",
          "Audit publish failed: topic cards.audit not available (broker kafka-2.ift.local:9093)")
    + _akka(_at(11, 0), 700, "WARN", 4, "system/sharding/cardRegion",
            "Retry request for shard [17] homes from coordinator. [1] buffered messages.")
    + _akka(_at(11, 1), 20, "ERROR", 6, "user/push-notifier",
            "Push provider returned 429 Too Many Requests, quota exhausted for app "
            "lumen-mobile-uat")
)

_BG_OTHER = [
    _akka(_at(11, 1, 6), 104, "ERROR", 2, "user/ldap-sync",
          "Directory sync failed: LDAP bind to ldaps://dir.ift.local:636 rejected (49 - "
          "Invalid Credentials)"),
    _akka(_at(11, 1, 7), 233, "ERROR", 8, "user/metrics",
          "Metrics push to http://pushgw.ift.local:9091 failed: 503 Service Unavailable")
    + _akka(_at(11, 1, 7), 240, "WARN", 8, "user/metrics", "Dropping 1200 samples"),
    _akka(_at(11, 1, 8), 15, "WARN", 5, "user/card-cache", "Cache eviction took 1840 ms")
    + _akka(_at(11, 1, 8), 510, "ERROR", 9, "user/statement-scheduler",
            "Scheduled job statements-nightly failed: lock statements-nightly held by "
            "cards-1 since 2026-10-14T02:00:00Z"),
]


def _cards_log(sec: int, card: str, actor: int, line: str, background: str) -> str:
    return (
        _akka(_at(10, 59), 1, "INFO", 1, "user", "Cards service 2.12.4 started, cluster "
              "members: [akka://cards@10.70.1.4:25520]")
        + background
        + _akka(sec, 118, "INFO", actor, f"user/activation/act-{card}",
                f"Activation requested for card **** {card}")
        + _akka(sec, 403, "ERROR", actor, f"user/activation/act-{card}", line)
    )


def shared_assert_bg_v3() -> Case:
    b = LaunchBuilder(8105, "ift cards regress #8105")
    message = 'expected:<"ACTIVE"> but was:<"INACTIVE">'

    def add(group: str, cause: str, category: str, card: str, index: int, line: str,
            background: str, evidence: str) -> None:
        b.add_failure(
            group, cause=cause, category=category,
            name=f"activates card {card}", full_name="ru.finplat.cards.e2e.ActivationSpec",
            message=message,
            trace=java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                             ["ru.finplat.cards.e2e.ActivationSpec$1$2.invokeSuspend"
                              f"(ActivationSpec.kt:{48 + index})",
                              "io.kotest.core.spec.style.scopes.FunSpecRootScope$test$1"
                              ".invokeSuspend(FunSpecRootScope.kt:84)"]),
            step="Activate card",
            log=_cards_log(_at(11, 2, index * 13), card, 10 + index, line, background),
            log_name="cards-service.log", evidence=[evidence])

    for index, card in enumerate(["4417", "4425", "4433"]):
        add("hsm-key", "hsm-key-not-loaded", "окружение", card, index,
            f"Activation of card **** {card} failed: HSM error 0x21 key PVK-UAT-2026 is not "
            "loaded in partition CARDS", _BG_SHARED,
            "HSM error 0x21 key PVK-UAT-2026 is not loaded")
    for index, (card, client) in enumerate([("7710", "C-551204"), ("7728", "C-551233")]):
        add("watchlist", "test-client-watchlist", "данные", card, 3 + index,
            f"Activation of card **** {card} blocked: client {client} matched watchlist WL-117 "
            "(sanctions screening)", _BG_SHARED, "matched watchlist WL-117")
    for index, card in enumerate(["2290", "2304"]):
        add("limit-profile", "limit-profile-missing", "данные", card, 5 + index,
            f"Activation of card **** {card} failed: limit profile LP-DEBIT-STD-2 not found "
            "for product DEBIT_CLASSIC", _BG_OTHER[index],
            "limit profile LP-DEBIT-STD-2 not found")
    for index, card in enumerate(["3301", "3319", "3327"]):
        add("pin-set-event", "pin-set-event-missing", "приложение", card, 7 + index,
            f"Activation of card **** {card} rejected: transition ISSUED -> ACTIVE requires "
            "event PIN_SET, none received", _BG_OTHER[index],
            "transition ISSUED -> ACTIVE requires event PIN_SET, none received")

    return b.build("shared_assert_bg_v3")


# --------------------------------------------------------------------------------------------
# one_problem_noisy_v3: одна причина за разными симптомами, шумные логи


def _fxquote_log(index: int, sec: int, quote: str, pair: str, noise: list[str]) -> str:
    def at(offset: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{(index * 211 + offset * 37) % 1000:03d}"

    poller = f"feed-poller-{1 + index % 3}"
    worker = f"ThreadPoolExecutor-0_{index % 4}"
    line_no = (204, 231)[index % 2]
    lines = [
        f"INFO     {at(-60)} [MainThread] fxquote.app:31 fx-quote 4.8.2 started, "
        "feeds=['moex-uat', 'partner-feed']",
        f"ERROR    {at(-20)} [{poller}] fxquote.feeds.partner:118 Feed refresh failed: [SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired "
        "(_ssl.c:1006) url=https://rates.partner-feed.lan/v3/spot",
        *(f"ERROR    {at(-10 + n)} [{worker}] {text}" for n, text in enumerate(noise)),
        f"INFO     {at(0)} [{worker}] fxquote.api:77 POST /v2/quotes pair={pair} "
        f"request_id={_hex(f'req{index}', 16)}",
        f"ERROR    {at(1)} [{worker}] fxquote.quotes:{line_no} Quote {quote} rejected: no fresh "
        f"{pair} rate (age {16180 + index * 31} s, limit 120 s)",
        f"ERROR    {at(40)} [{poller}] fxquote.feeds.partner:118 Feed refresh failed: [SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired "
        "(_ssl.c:1006) url=https://rates.partner-feed.lan/v3/spot",
    ]
    return "\n".join(lines) + "\n"


_FX_NOISE = [
    ["audit.sink:44 Failed to flush 12 audit records: SlowDown (bucket audit-uat)"],
    ["mail.digest:90 SMTP 421 4.7.0 Try again later, digest for desk-7 postponed",
     "audit.sink:44 Failed to flush 3 audit records: SlowDown (bucket audit-uat)"],
    [],
    ["limits.sync:15 Limits snapshot is 2 versions behind, resync scheduled"],
    ["metrics.push:22 push to pushgw.ift.local:9091 failed: HTTP 503"],
    ["clients.cache:61 client 88120 not found in cache"],
    ["audit.sink:44 Failed to flush 7 audit records: SlowDown (bucket audit-uat)",
     "metrics.push:22 push to pushgw.ift.local:9091 failed: HTTP 503"],
]


def one_problem_noisy_v3() -> Case:
    b = LaunchBuilder(8106, "uat fx regress #8106")
    cause, category = "fx-feed-cert-expired", "окружение"
    evidence = ("certificate has expired (_ssl.c:1006) "
                "url=https://rates.partner-feed.lan/v3/spot")

    robot = [("EUR/RUB", "q-88120"), ("USD/RUB", "q-88173"), ("CNY/RUB", "q-88190"),
             ("EUR/USD", None)]
    for index, (pair, quote) in enumerate(robot):
        log = (_fxquote_log(index, _at(11, 10 + index * 2), quote, pair, _FX_NOISE[index])
               if quote else None)
        b.add_failure(
            "fx-quote-status", cause=cause, category=category,
            name=f"Quote Is Confirmed For {pair}", full_name="Fx Quotes.Spot Quotes",
            message="Quote status: REJECTED != CONFIRMED", step="Request FX quote",
            log=log, log_name="fx-quote.log", evidence=[evidence] if quote else [])

    for index, (pair, quote) in enumerate([("EUR/RUB", "q-88211"), ("GBP/RUB", "q-88240")]):
        b.add_failure(
            "fx-quote-client-422", cause=cause, category=category,
            name=f"CreateQuoteViaApi ({pair})",
            full_name="Fx.E2E.QuoteApiTests.CreateQuoteViaApi",
            message="Refit.ApiException: Response status code does not indicate success: 422 "
                    "(Unprocessable Entity).",
            trace="Refit.ApiException: Response status code does not indicate success: 422 "
                  "(Unprocessable Entity).\n"
                  "   at Refit.RequestBuilderImplementation.<>c__DisplayClass14_0`2."
                  "<<BuildCancellableTaskFuncForMethod>b__0>d.MoveNext() in "
                  "/_/Refit/RequestBuilderImplementation.cs:line 288\n"
                  "   at Fx.E2E.Support.FxQuoteClient.CreateAsync(QuoteRequest request) in "
                  "/src/Fx.E2E/Support/FxQuoteClient.cs:line 29\n"
                  "   at Fx.E2E.QuoteApiTests.CreateQuoteViaApi(String pair) in "
                  f"/src/Fx.E2E/QuoteApiTests.cs:line {41 + index * 18}\n",
            step="POST /v2/quotes",
            log=_fxquote_log(4 + index, _at(11, 20 + index * 3), quote, pair,
                             _FX_NOISE[4 + index]),
            log_name="fx-quote.log", evidence=[evidence])

    for index, (pair, quote) in enumerate([("EUR/RUB", "q-88302"), ("USD/RUB", "q-88317")]):
        b.add_failure(
            "fx-quote-ui-wait", cause=cause, category=category,
            name=f"Desk shows quote amount for {pair}",
            full_name="tests/desk/quote.js Desk shows quote amount",
            message='Timed out while waiting for element <[data-test="fx-quote-amount"]> to be '
                    'present for 10000 milliseconds. - expected "present" but got: "not present" '
                    f"({10040 + index * 17}ms)",
            step="Open FX desk quote panel",
            log=_fxquote_log(6 + index, _at(11, 30 + index * 4), quote, pair,
                             _FX_NOISE[6 - index]),
            log_name="fx-quote.log", evidence=[evidence])

    return b.build("one_problem_noisy_v3")


# --------------------------------------------------------------------------------------------
# one_problem_resources_v3: одна причина, разные ресурсы в сообщении


def _notif_events(pod: str) -> str:
    return (
        "LAST SEEN   TYPE      REASON    OBJECT                                     MESSAGE\n"
        f"4m          Normal    Pulled    pod/{pod}      Container image "
        '"registry.uat.internal/notif-dispatcher:1.18.0" already present on machine\n'
        f'3m58s       Warning   Failed    pod/{pod}      Error: secret "fcm-credentials" not '
        "found\n"
        f"12s         Normal    Pulled    pod/{pod}      Container image "
        '"registry.uat.internal/notif-dispatcher:1.18.0" already present on machine\n'
    )


def _smsgw_log(sec: int, user: str) -> str:
    def at(offset: int, ms: int) -> str:
        return f"[2026-10-14 {_hms(sec + offset)},{ms:03d}]"

    return (
        f"{at(-5, 118)} INFO SMPP session sms-uat-1 connecting to smsc.uat.internal:2775 "
        "(ru.finplat.smsgw.smpp.SessionManager)\n"
        f"{at(-5, 231)} ERROR SMPP bind_transceiver failed for system_id=finplat_uat: "
        "command_status=0x0000000E (ESME_RINVPASWD) (ru.finplat.smsgw.smpp.SessionManager)\n"
        f"{at(0, 640)} INFO OTP requested for {user}, channel=sms "
        "(ru.finplat.smsgw.OtpSender)\n"
        f"{at(0, 642)} WARN OTP message for {user} queued, no bound SMPP session "
        "(ru.finplat.smsgw.OtpSender)\n"
        f"{at(25, 231)} ERROR SMPP bind_transceiver failed for system_id=finplat_uat: "
        "command_status=0x0000000E (ESME_RINVPASWD) (ru.finplat.smsgw.smpp.SessionManager)\n"
    )


def _labels_log(order: str, jid: str, sec: int) -> str:
    stamp = f"2026-10-14T{_hms(sec)}"
    head = f"pid=12 tid=8h{jid[:2]} class=ShippingLabelJob jid={jid}"
    return (
        f"{stamp}.511Z {head} INFO: start\n"
        f"{stamp}.602Z {head} elapsed=0.091 INFO: fail\n"
        f'{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: {{"context":"Job raised exception",'
        f'"job":{{"retry":5,"queue":"labels","class":"ShippingLabelJob","args":["{order}"],'
        f'"jid":"{jid}"}}}}\n'
        f"{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: Carriers::AuthError: carrier API rejected "
        "label request: 401 Unauthorized (api key ck_uat_7Q2m expired on 2026-10-13)\n"
        f"{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: /app/app/services/carriers/client.rb:41:in "
        "'Carriers::Client#create_label'\n"
    )


def _chrome_console(asset: str) -> str:
    return (
        f"GET https://ib.ift.local/assets/{asset}.css net::ERR_ABORTED 404 (Not Found)\n"
        f"GET https://ib.ift.local/assets/{asset}.js net::ERR_ABORTED 404 (Not Found)\n"
        f"Uncaught (in promise) Error: Unable to preload CSS for /assets/{asset}.css\n"
        "    at HTMLLinkElement.<anonymous> (https://ib.ift.local/assets/index-4c22e1a9.js:1:"
        "1288)\n"
    )


def one_problem_resources_v3() -> Case:
    b = LaunchBuilder(8107, "ift regress #8107")

    for index, (address, pod) in enumerate([
        ("10.62.8.14", "notif-dispatcher-6c9f7d8b4-tq2lm"),
        ("10.62.9.3", "notif-dispatcher-6c9f7d8b4-8xw7d"),
        ("10.62.11.27", "notif-dispatcher-6c9f7d8b4-vb5nk"),
    ]):
        b.add_failure(
            "notif-pods-refused", cause="notif-dispatcher-secret-missing", category="окружение",
            name=f"TestSendPush/{['card_debit', 'login_alert', 'transfer_in'][index]}",
            full_name="notify/e2e.TestSendPush",
            message=f"notify_test.go:{41 + index}: send push: rpc error: code = Unavailable "
                    'desc = connection error: desc = "transport: Error while dialing: dial tcp '
                    f'{address}:9090: connect: connection refused"',
            step="Send push notification", log=_notif_events(pod), log_name="k8s-events.txt",
            evidence=['Error: secret "fcm-credentials" not found'])

    for index, user in enumerate(["k.orlova", "d.mironov", "a.zueva"]):
        message = f"OTP for user '{user}' was not received within 60 s"
        b.add_failure(
            "otp-not-received", cause="smpp-bind-password", category="окружение",
            name=f"login with OTP as {user}", full_name="ru.finplat.auth.e2e.OtpLoginSpec",
            message=message,
            trace=java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                             ["ru.finplat.auth.e2e.OtpLoginSpec$1$1.invokeSuspend"
                              f"(OtpLoginSpec.kt:{33 + index})"]),
            step="Wait for OTP", log=_smsgw_log(_at(11, 20, index * 40), user),
            log_name="sms-gateway.log",
            evidence=["bind_transceiver failed for system_id=finplat_uat: "
                      "command_status=0x0000000E (ESME_RINVPASWD)"])

    for index, order in enumerate(["A-77120", "A-77134", "A-77141"]):
        jid = _hex(f"label{order}", 24)
        b.add_failure(
            "order-not-shipped", cause="carrier-api-key-expired", category="окружение",
            name=f"test_ships_order_{index + 1}", full_name="ShippingFlowTest#test_ships_order",
            message=f'Order {order} should be shipped.\nExpected: "shipped"\n'
                    '  Actual: "awaiting_label"',
            trace=f"test/system/shipping_flow_test.rb:{19 + index * 6}:in "
                  "'ShippingFlowTest#test_ships_order'\n",
            step="Ship order", log=_labels_log(order, jid, _at(11, 22, index * 31)),
            log_name="sidekiq.log",
            evidence=["carrier API rejected label request: 401 Unauthorized"])

    cause = "stand-dns-failure"
    for index, (host, doc) in enumerate([("docs-store.ift.local", "/api/v1/docs/D-4410"),
                                         ("audit-trail.ift.local", "/api/v1/events?doc=D-4410")]):
        b.add_failure(
            "dns-go-servfail", cause=cause, category="окружение",
            name=f"TestFetchDocument/{['body', 'audit'][index]}",
            full_name="docs/e2e.TestFetchDocument",
            message=f'docs_test.go:{29 + index * 8}: fetch: Get "https://{host}{doc}": dial tcp: '
                    f"lookup {host} on 10.62.0.10:53: server misbehaving",
            step="Fetch document", evidence=["on 10.62.0.10:53: server misbehaving"])
    for index, host in enumerate(["kyc-files.ift.local", "bonus-api.ift.local"]):
        b.add_failure(
            "dns-node-notfound", cause=cause, category="окружение",
            name=["downloads KYC file", "reads bonus balance"][index],
            full_name=f"test/{['kyc', 'bonus'][index]}.test.js",
            message=f"RequestError: getaddrinfo ENOTFOUND {host}",
            trace=f"RequestError: getaddrinfo ENOTFOUND {host}\n"
                  "    at ClientRequest.<anonymous> (node_modules/got/dist/source/core/index.js:"
                  "792:61)\n"
                  "    at GetAddrInfoReqWrap.onlookupall [as oncomplete] (node:dns:120:26)\n",
            step="call service", evidence=[f"getaddrinfo ENOTFOUND {host}"])
    b.add_failure(
        "dns-rust-lookup", cause=cause, category="окружение",
        name="rates::history_is_loaded", full_name="fx_history::tests::rates",
        message="thread 'rates::history_is_loaded' panicked at tests/rates.rs:22:10:\n"
                "called `Result::unwrap()` on an `Err` value: reqwest::Error { kind: Request, "
                'url: "https://fx-history.ift.local/api/rates", source: '
                'hyper_util::client::legacy::Error(Connect, ConnectError("dns error", Custom { '
                'kind: Uncategorized, error: "failed to lookup address information: Try again" '
                "})) }",
        evidence=['ConnectError("dns error"', "failed to lookup address information: Try again"])

    asset = "DepositCalculator-7b31c0e2"
    for index, locator in enumerate(['[data-cy="deposit-rate"]', '[data-cy="deposit-term"]',
                                     '[data-cy="deposit-submit"]']):
        message, trace = _cypress_get(locator, "deposits/calculator.cy.ts", 18 + index * 15)
        b.add_failure(
            "deposit-page-not-loaded", cause="deposit-bundle-missing", category="приложение",
            name=["shows rate", "changes term", "submits application"][index],
            full_name="cypress/e2e/deposits/calculator.cy.ts Deposit calculator",
            message=message, trace=trace, step="Open deposit calculator",
            logs=[("browser-console.log", _chrome_console(asset), "text/plain")],
            evidence=[f"{asset}.js net::ERR_ABORTED 404 (Not Found)"])

    return b.build("one_problem_resources_v3")


# --------------------------------------------------------------------------------------------
# signatures_kb_v3: близкие тексты разных проблем, причины с несколькими симптомами


def _wildfly(sec: int, ms: int, level: str, category: str, thread: str, text: str) -> str:
    return f"{_hms(sec)},{ms:03d} {level:<5} [{category}] ({thread}) {text}\n"


def _loans_log(sec: int, task: int, body: str) -> str:
    return (
        _wildfly(sec - 300, 4, "INFO", "org.jboss.as", "Controller Boot Thread",
                 "WFLYSRV0025: WildFly Full 31.0.1.Final (WildFly Core 23.0.3.Final) started "
                 "in 8412ms - Started 612 of 838 services")
        + _wildfly(sec, 101, "INFO", "com.meridian.loans.api.ApplicationResource",
                   f"default task-{task}", "application received")
        + body
    )


def _python_report_log(sec: int, day: str) -> str:
    return (
        f"INFO     2026-10-14 {_hms(sec)},004 [MainThread] reports.app:22 insurance-reports 1.6.0 "
        "started\n"
        f"INFO     2026-10-14 {_hms(sec + 3)},118 [worker-1] reports.insurance:51 build report "
        f"day={day}\n"
        f"ERROR    2026-10-14 {_hms(sec + 3)},301 [worker-1] reports.insurance:77 Report build "
        "failed: (psycopg.errors.UndefinedColumn) column loan_offers.insurance_type does not "
        "exist\n"
        "LINE 1: SELECT loan_offers.id, loan_offers.insurance_type, loan_offers.premium FROM "
        "loan_offers ...\n"
        "                               ^\n"
        "[SQL: SELECT loan_offers.id, loan_offers.insurance_type, loan_offers.premium FROM "
        "loan_offers WHERE loan_offers.issued_on = %(day)s]\n"
    )


def _s3_denied(sec: int, task: int, key: str) -> str:
    return _wildfly(sec + 1, 412, "ERROR", "com.meridian.loans.docs.DocumentStore",
                    f"default task-{task}",
                    f"S3 PutObject to bucket loan-docs-uat failed (key={key}): Access Denied "
                    f"(Service: S3, Status Code: 403, Request ID: {_hex(key, 16).upper()})")


def signatures_kb_v3() -> Case:
    b = LaunchBuilder(8108, "ift loans regress #8108")
    declined = "AssertionError: expected 'DECLINED' to be 'APPROVED' // Object.is equality"

    def vitest(title: str, spec: str, line: int, message: str) -> dict[str, str]:
        return {"name": title, "full_name": f"tests/{spec} > {title}", "message": message,
                "trace": f"{message}\n    at /builds/loans/api-tests/tests/{spec}:{line}:41\n"}

    for index, app in enumerate(["LA-50112", "LA-50131"]):
        b.add_failure(
            "scoring-model", cause="scoring-model-not-deployed", category="окружение",
            step="Submit loan application",
            log=_loans_log(_at(10, 30, index * 20), 4 + index, _wildfly(
                _at(10, 30, index * 20 + 1), 114, "ERROR",
                "com.meridian.loans.scoring.ModelRegistry", f"default task-{4 + index}",
                "Scoring model SCR-2026-Q4 is not deployed, fallback policy DECLINE_ALL applied "
                f"to application {app}")),
            log_name="loans-server.log",
            evidence=["Scoring model SCR-2026-Q4 is not deployed"],
            **vitest(f"approves cash loan {index + 1}", "loans/cash.spec.ts", 27 + index * 9,
                     declined))

    bureau_logs = [
        _loans_log(_at(10, 32), 9,
                   _wildfly(_at(10, 32, 20), 3, "WARN", "com.meridian.loans.bureau.BureauClient",
                            "default task-9", "Credit bureau request for LA-50150 timed out "
                            "after 20000 ms (bureau=NBKI-UAT)")
                   + _wildfly(_at(10, 32, 20), 9, "INFO",
                              "com.meridian.loans.decision.DecisionEngine", "default task-9",
                              "application LA-50150 decision DECLINED: bureau report unavailable")),
        _wildfly(_at(10, 33, 1), 220, "INFO", "com.meridian.loans.bureau.BureauPool",
                 "bureau-pool-3", "pool stats: active=8 idle=0 waiting=14")
        + _wildfly(_at(10, 33, 21), 518, "ERROR", "com.meridian.loans.bureau.BureauClient",
                   "bureau-pool-3", "Bureau NBKI-UAT did not respond within 20000 ms, request "
                   "BR-99812 for LA-50177 abandoned")
        + _wildfly(_at(10, 33, 21), 521, "WARN", "com.meridian.loans.bureau.BureauPool",
                   "bureau-pool-3", "circuit nbki-uat half-open, 3 of 5 recent calls failed")
        + _wildfly(_at(10, 33, 21), 530, "INFO", "com.meridian.loans.decision.DecisionEngine",
                   "default task-2", "application LA-50177 decision DECLINED: bureau report "
                   "unavailable"),
        "10.44.20.7:53122 [14/Oct/2026:10:34:02.118] fe_bureau be_bureau/nbki-uat 0/0/1/-1/20002 "
        '504 198 - - sH-- 31/31/14/14/0 0/0 "POST /nbki/v2/report HTTP/1.1"\n'
        "10.44.20.7:53140 [14/Oct/2026:10:34:05.440] fe_bureau be_bureau/nbki-uat 0/0/1/-1/20001 "
        '504 198 - - sH-- 30/30/13/13/0 0/0 "POST /nbki/v2/report HTTP/1.1"\n',
    ]
    for index, log in enumerate(bureau_logs):
        b.add_failure(
            "bureau-timeout", cause="bureau-timeout", category="окружение",
            step="Submit loan application", log=log,
            log_name=["loans-server.log", "loans-server.log", "haproxy.log"][index],
            evidence=["decision DECLINED: bureau report unavailable"] if index == 0 else
            (["Bureau NBKI-UAT did not respond within 20000 ms"] if index == 1 else []),
            **vitest(f"approves card loan {index + 1}", "loans/card.spec.ts", 19 + index * 7,
                     declined))

    b.add_failure(
        "applicant-age", cause="applicant-underage", category="данные",
        step="Submit loan application",
        log=_loans_log(_at(10, 35), 3, _wildfly(
            _at(10, 35, 1), 77, "INFO", "com.meridian.loans.decision.PolicyRules",
            "default task-3", "Applicant age 17 is below minimum 18 for product CONSUMER_CASH, "
            "application LA-50190 DECLINED")),
        log_name="loans-server.log",
        evidence=["Applicant age 17 is below minimum 18 for product CONSUMER_CASH"],
        **vitest("approves student loan", "loans/student.spec.ts", 14, declined))

    s3 = "S3 PutObject to bucket loan-docs-uat failed"
    uploaded = "AssertionError: expected 'FAILED' to be 'UPLOADED' // Object.is equality"
    for index, key in enumerate(["LA-50201/income-2025.pdf", "LA-50214/income-2025.pdf"]):
        sec = _at(10, 40, index * 15)
        b.add_failure(
            "upload-status-failed", cause="doc-storage-access-denied", category="окружение",
            step="Upload income certificate",
            log=_loans_log(sec, 11 + index, _s3_denied(sec, 11 + index, key)),
            log_name="loans-server.log", evidence=[s3],
            **vitest(f"uploads income certificate {index + 1}", "docs/upload.spec.ts",
                     33 + index * 6, uploaded))
    for index, key in enumerate(["LA-50230/passport.jpg", "LA-50236/passport.jpg"]):
        sec = _at(10, 41, index * 15)
        b.add_failure(
            "upload-ui-success-missing", cause="doc-storage-access-denied", category="окружение",
            name=f"Passport copy is attached ({index + 1})",
            full_name="Documents / Passport copy is attached",
            message="The specified selector does not match any element in the DOM tree.\n\n"
                    " > | Selector('.upload-status--success')",
            step="Attach passport copy",
            log=_loans_log(sec, 14 + index, _s3_denied(sec, 14 + index, key)),
            log_name="loans-server.log", evidence=[s3])
    for index, key in enumerate(["LA-50251/contract.pdf", "LA-50266/contract.pdf"]):
        sec = _at(10, 42, index * 15)
        message = ('Server error(POST http://loan-docs.ift.local/api/v1/documents: 500 Internal '
                   'Server Error. Text: "{"code":"STORAGE_UNAVAILABLE"}"')
        b.add_failure(
            "upload-api-500", cause="doc-storage-access-denied", category="окружение",
            name=f"uploads signed contract {index + 1}",
            full_name="ru.meridian.loans.e2e.DocumentsApiSpec", message=message,
            trace=java_trace(f"io.ktor.client.plugins.ServerResponseException: {message}",
                             ["io.ktor.client.plugins.DefaultResponseValidationKt$addDefault"
                              "ResponseValidation$1$1.invokeSuspend(DefaultResponseValidation.kt"
                              ":54)",
                              "ru.meridian.loans.e2e.DocumentsApiSpec$1$1.invokeSuspend"
                              f"(DocumentsApiSpec.kt:{40 + index * 11})"]),
            step="Upload document via API",
            log=_loans_log(sec, 17 + index, _s3_denied(sec, 17 + index, key)),
            log_name="loans-server.log", evidence=[s3])
    for index, app in enumerate(["LA-50280", "LA-50293"]):
        sec = _at(10, 44, index * 15)
        b.add_failure(
            "upload-eicar", cause="test-file-eicar", category="данные",
            step="Upload income certificate",
            log=_loans_log(sec, 3 + index, _wildfly(
                sec + 1, 77, "WARN", "com.meridian.loans.docs.AvScanner", f"default task-{3 + index}",
                f"File {app}/income-2024.pdf rejected by antivirus: Win.Test.EICAR_HDB-1 FOUND")),
            log_name="loans-server.log",
            evidence=["rejected by antivirus: Win.Test.EICAR_HDB-1 FOUND"],
            **vitest(f"uploads last year income certificate {index + 1}", "docs/upload.spec.ts",
                     61 + index * 6, uploaded))

    column = "ERROR: column lo1_0.insurance_type does not exist"

    def hibernate_body(sec: int, task: int) -> str:
        return (
            _wildfly(sec + 1, 300, "WARN", "org.hibernate.engine.jdbc.spi.SqlExceptionHelper",
                     f"default task-{task}", "SQL Error: 0, SQLState: 42703")
            + _wildfly(sec + 1, 301, "ERROR", "org.hibernate.engine.jdbc.spi.SqlExceptionHelper",
                       f"default task-{task}", column)
            + "  Position: 87\n"
        )

    for index in range(2):
        sec = _at(10, 50, index * 20)
        b.add_failure(
            "insurance-offer-500", cause="insurance-type-migration", category="приложение",
            step="Create insurance offer",
            log=_loans_log(sec, 21 + index, hibernate_body(sec, 21 + index)),
            log_name="loans-server.log", evidence=[column],
            **vitest(f"creates life insurance offer {index + 1}", "insurance/offer.spec.ts",
                     22 + index * 8, "AssertionError: expected 500 to be 201 // Object.is "
                                     "equality"))
    for index, day in enumerate(["2026-10-13", "2026-10-12"]):
        b.add_failure(
            "insurance-report-empty", cause="insurance-type-migration", category="приложение",
            name=f"Insurance report for {day}", full_name="features/reports.feature",
            message=f"Assertion Failed: insurance report for {day} has 0 rows, expected > 0",
            step="Then the insurance report has rows",
            log=_python_report_log(_at(10, 52, index * 30), day), log_name="reports.log",
            evidence=["(psycopg.errors.UndefinedColumn) column loan_offers.insurance_type does "
                      "not exist"])
    sec = _at(10, 54)
    b.add_failure(
        "insurance-offer-ui", cause="insurance-type-migration", category="приложение",
        name="Insurance offer is shown", full_name="Insurance / Insurance offer is shown",
        message="The specified selector does not match any element in the DOM tree.\n\n"
                " > | Selector('[data-testid=\"insurance-offer\"]')",
        step="Open insurance offer", log=_loans_log(sec, 25, hibernate_body(sec, 25)),
        log_name="loans-server.log", evidence=[column])

    return b.build("signatures_kb_v3")


# --------------------------------------------------------------------------------------------
# retries_v3: повторы Cypress, связь через historyId, historyKey и testCaseId + parameters


def _cy_request(url: str, status: str) -> str:
    return (f"cy.request() failed on:\n\n{url}\n\nThe response we received from your web server "
            f"was:\n\n  > {status}\n\nThis was considered a failure because the status code "
            "was not `2xx` or `3xx`.")


def retries_v3() -> Case:
    b = LaunchBuilder(8109, "policy-portal e2e #8109")
    api = "https://polis-gw.ift.local/api/v2"
    env = [{"name": "stand", "value": "ift"}]
    quote_500 = _cy_request(f"{api}/quotes", "500: Internal Server Error")

    def result(name: str, status: str, message: str | None, step: str, extra: dict[str, Any],
               hidden: bool = False) -> int:
        return b.add_result(name=name, status=status, message=message, step=step, hidden=hidden,
                            full_name=f"cypress/e2e/policy/{name.split(' ')[0]}.cy.ts {name}",
                            extra=extra)

    name = "quote shows premium"
    link = {"historyId": _hex("h-premium"), "testCaseId": 12001}
    result(name, "failed", "AssertionError: Timed out retrying after 4000ms: Expected to find "
           'element: `[data-cy="quote-premium"]`, but never found it.', "Calculate quote", link,
           hidden=True)
    passed = result(name, "passed", None, "Calculate quote", link)
    b.expect_passed_after_retry(passed)

    name = "quote for new driver"
    link = {"historyKey": _hex("k-new-driver", 40)}
    first = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
    second = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
    final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                          step="Calculate quote", extra=link,
                          full_name="cypress/e2e/policy/quote.cy.ts quote for new driver")
    b.expect_attempts(final, [(first, True), (second, True)])

    name = "policy is issued"
    link = {"historyId": _hex("h-issue"), "testCaseId": 12003}
    first = result(name, "failed", "AssertionError: Timed out retrying after 4000ms: expected "
                   "'<span.policy-status>' to contain text 'Issued', but the text was "
                   "'Processing'", "Issue contract and wait", link, hidden=True)
    final = b.add_failure("issue-502", cause=None, name=name,
                          message=_cy_request(f"{api}/policies/issue", "502: Bad Gateway"),
                          step="Issue contract and wait", extra=link,
                          full_name="cypress/e2e/policy/issue.cy.ts policy is issued")
    b.expect_attempts(final, [(first, False)])

    for product in ["OSAGO", "KASKO"]:
        name = "quote by product"
        link = {"testCaseId": 12004, "parameters": [{"name": "product", "value": product}],
                "environment": env}
        attempt = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
        final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                              step="Calculate quote", extra=link,
                              full_name="cypress/e2e/policy/quote.cy.ts quote by product")
        b.expect_attempts(final, [(attempt, True)])

    name = "status turns issued"
    status_message = ("AssertionError: Timed out retrying after 4000ms: expected 'Processing' to "
                      "equal 'Issued'")
    link = {"historyId": _hex("h-status"), "testCaseId": 12005}
    attempt = result(name, "failed", None, "Wait for policy status", link, hidden=True)
    b.add_detail(attempt, message=status_message)
    final = b.add_failure("status-processing", cause=None, name=name, message=status_message,
                          step="Wait for policy status", extra=link,
                          full_name="cypress/e2e/policy/status.cy.ts status turns issued")
    b.expect_attempts(final, [(attempt, True)])

    name = "pay premium via SBP"
    sbp_503 = _cy_request(f"{api}/payments/sbp", "503: Service Unavailable")
    link = {"historyId": _hex("h-sbp"), "testCaseId": 12006}
    attempts = [result(name, "failed", sbp_503, "Pay premium", link, hidden=True)
                for _ in range(6)]
    final = b.add_failure("sbp-503", cause=None, name=name, message=sbp_503, step="Pay premium",
                          extra=link, full_name="cypress/e2e/policy/pay.cy.ts pay premium via SBP")
    b.expect_attempts(final, [(attempt, True) for attempt in attempts])

    name = "agent sees commission"
    commission = ("AssertionError: Timed out retrying after 4000ms: Expected to find element: "
                  '`[data-cy="agent-commission"]`, but never found it.')
    for _ in range(2):
        b.add_result(name=name, status="failed", message=commission, step="Open agent cabinet",
                     hidden=True)
    final = b.add_failure("commission-missing", cause=None, name=name, message=commission,
                          step="Open agent cabinet", extra={"historyId": _hex("h-commission")},
                          full_name="cypress/e2e/policy/agent.cy.ts agent sees commission")
    b.expect_attempts(final, [])

    name = "quote for young driver"
    final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                          step="Calculate quote",
                          extra={"historyId": _hex("h-young"), "testCaseId": 12008},
                          full_name="cypress/e2e/policy/quote.cy.ts quote for young driver")
    b.expect_attempts(final, [])

    return b.build("retries_v3")


# --------------------------------------------------------------------------------------------
# retries_env_v3: без historyId и historyKey, связь только testCaseId + parameters + environment


def _karate_status(code: int, expected: int, host: str, path: str, body: str, ms: int) -> str:
    return (f"status code was: {code}, expected: {expected}, response time in milliseconds: "
            f"{ms}, url: https://{host}{path}, response: \n{body}")


def retries_env_v3() -> Case:
    b = LaunchBuilder(8110, "acquiring regress #8110")

    def env(stand: str) -> list[dict[str, str]]:
        return [{"name": "stand", "value": stand}]

    def extra(case_id: int, params: list[dict[str, str]], stand: str) -> dict[str, Any]:
        return {"testCaseId": case_id, "parameters": params, "environment": env(stand)}

    def hidden(name: str, message: str, link: dict[str, Any]) -> int:
        return b.add_result(name=name, status="failed", message=message, hidden=True,
                            full_name=f"classpath:acquiring/{name}.feature", extra=link)

    terminal = [{"name": "terminal", "value": "T-5501"}]
    settle = "settle"
    settle_503 = _karate_status(503, 200, "acquiring.ift-1.local", "/api/v1/terminals/T-5501/"
                                "settle", '{"error":"settlement window closed"}', 30012)
    attempt = hidden(settle, settle_503, extra(9101, terminal, "ift-1"))
    final = b.add_failure("settle-503", cause=None, name=settle, message=settle_503,
                          full_name="classpath:acquiring/settle.feature",
                          extra=extra(9101, terminal, "ift-1"))
    b.expect_attempts(final, [(attempt, True)])
    final = b.add_failure(
        "settle-401", cause=None, name=settle,
        message=_karate_status(401, 200, "acquiring.ift-2.local", "/api/v1/terminals/T-5501/"
                               "settle", '{"error":"invalid_token"}', 41),
        full_name="classpath:acquiring/settle.feature", extra=extra(9101, terminal, "ift-2"))
    b.expect_attempts(final, [])

    refund = "refund"
    hidden(refund, _karate_status(503, 200, "acquiring.ift-1.local", "/api/v1/refunds",
                                  '{"error":"upstream unavailable"}', 30007),
           extra(9102, [], "ift-1"))
    final = b.add_failure(
        "refund-409", cause=None, name=refund,
        message=_karate_status(409, 200, "acquiring.ift-2.local", "/api/v1/refunds",
                               '{"error":"refund already exists"}', 88),
        full_name="classpath:acquiring/refund.feature", extra=extra(9102, [], "ift-2"))
    b.expect_attempts(final, [])

    convert = "convert"
    rate_zero = "match failed: EQUALS\n  $.rate | not equal (NUMBER:NUMBER)\n  0\n  92.15"
    rub = [{"name": "currency", "value": "RUB"}]
    attempt = hidden(convert, rate_zero, extra(9103, rub, "ift-1"))
    final = b.add_failure("fx-rate-zero", cause=None, name=convert, message=rate_zero,
                          full_name="classpath:acquiring/convert.feature",
                          extra=extra(9103, rub, "ift-1"))
    b.expect_attempts(final, [(attempt, True)])
    usd = [{"name": "currency", "value": "USD"}]
    attempt = hidden(convert, rate_zero.replace("92.15", "1.0"), extra(9103, usd, "ift-1"))
    final = b.add_failure(
        "fx-convert-500", cause=None, name=convert,
        message=_karate_status(500, 200, "acquiring.ift-1.local", "/api/v1/fx/convert",
                               '{"error":"internal"}', 120),
        full_name="classpath:acquiring/convert.feature", extra=extra(9103, usd, "ift-1"))
    b.expect_attempts(final, [(attempt, False)])

    payout = "payout"
    hidden(payout, _karate_status(504, 200, "acquiring.ift-2.local", "/api/v1/payouts",
                                  '{"error":"gateway timeout"}', 60001),
           extra(9104, [], "ift-2"))
    passed = b.add_result(name=payout, status="passed", extra=extra(9104, [], "ift-2"),
                          full_name="classpath:acquiring/payout.feature")
    b.expect_passed_after_retry(passed)

    return b.build("retries_env_v3")


# --------------------------------------------------------------------------------------------
# sparse_data_v3: падения без данных, только шаг, только лог; muted, hidden, passed


def _rabbit_log(sec: int, conn: str, port: int) -> str:
    stamp = f"2026-10-14 {_hms(sec)}.902114+00:00"
    return (
        "2026-10-14 12:00:00.101418+00:00 [info] <0.230.0> Server startup complete; 4 plugins "
        "started.\n"
        f"{stamp} [info] <0.{conn}.0> accepting AMQP connection 10.44.8.21:{port} -> "
        "10.44.8.5:5672\n"
        f"{stamp} [error] <0.{conn}.0> Channel error on connection <0.{conn}.0> "
        f"(10.44.8.21:{port} -> 10.44.8.5:5672, vhost: 'uat', user: 'loan-events'), channel 1:\n"
        f"{stamp} [error] <0.{conn}.0> operation basic.publish caused a channel exception "
        "not_found: no exchange 'loan.decisions.v2' in vhost 'uat'\n"
    )


def sparse_data_v3() -> Case:
    b = LaunchBuilder(8111, "mobile-bank smoke #8111")
    suite = "LumenBankUITests"

    for index, (test, status) in enumerate([("testOpenDepositsTab", "failed"),
                                            ("testChangeLanguage", "broken"),
                                            ("testLogoutClearsCache", "failed")]):
        b.add_failure(f"silent-{index + 1}", cause=None, name=test, status=status,
                      full_name=f"{suite}.SmokeTests.{test}")

    for test in ["testSignCreditAgreement", "testSignCardAgreement"]:
        b.add_failure("step-sign-agreement", cause=None, name=test,
                      full_name=f"{suite}.AgreementTests.{test}",
                      step="Подписать кредитный договор")
    b.add_failure("step-card-limits", cause=None, name="testCardLimitsScreen",
                  full_name=f"{suite}.CardTests.testCardLimitsScreen",
                  step="Open card limits screen")

    for index, test in enumerate(["testLoanDecisionPublished", "testLoanDecisionNotified"]):
        b.add_failure(
            "exchange-missing", cause="loan-exchange-missing", category="окружение",
            name=test, full_name=f"{suite}.LoanEventsTests.{test}",
            log=_rabbit_log(_at(12, 5, 11 + index * 9), str(4412 + index * 37), 50122 + index),
            log_name="rabbitmq.log",
            evidence=["no exchange 'loan.decisions.v2' in vhost 'uat'"])

    for test in ["testPushOnLogin", "testBiometricPrompt"]:
        b.add_result(name=test, status="failed", muted=True,
                     message="XCTAssertTrue failed - push banner did not appear",
                     full_name=f"{suite}.KnownIssuesTests.{test}")
    for test in ["testTransferToCard", "testPayMobile"]:
        b.add_result(name=test, status="failed", hidden=True,
                     message="Failed to get matching snapshot: No matches found for Elements "
                             'matching predicate \'"confirm_button" IN identifiers\'',
                     full_name=f"{suite}.PaymentsTests.{test}")
        b.add_result(name=test, status="passed", full_name=f"{suite}.PaymentsTests.{test}")
    for test in ["testLogin", "testBalanceShown", "testHistoryScroll"]:
        b.add_result(name=test, status="passed", full_name=f"{suite}.SmokeTests.{test}")
    b.add_result(name="testApplePay", status="skipped", full_name=f"{suite}.PaymentsTests"
                 ".testApplePay")

    return b.build("sparse_data_v3")


# --------------------------------------------------------------------------------------------
# operation_logs_v3: общие логи сервисов, операции помечены rqUID/operUID и traceparent


def _payhub_line(sec: int, ms: int, level: str, category: str, thread: str,
                 mdc: str | None, text: str) -> str:
    ids = f"[{mdc}] " if mdc else ""
    return (f"2026-10-14 {_hms(sec)},{ms:03d} {level:<5} [ru.finplat.payhub.{category}] "
            f"({thread}) {ids}{text}\n")


def _payhub_log(ops: list[dict[str, Any]]) -> str:
    lines = [
        "2026-10-14 12:20:00,412 INFO  [org.jboss.as] (Controller Boot Thread) WFLYSRV0025: "
        "WildFly Full 31.0.1.Final (WildFly Core 23.0.3.Final) started in 8412ms\n",
        _payhub_line(_at(12, 20, 1), 5, "INFO", "Bootstrap", "ServerService Thread Pool -- 74",
                     None, "payhub 3.12.0 started, profile=ift, abs=abs-adapter.ift.local"),
        _payhub_line(_at(12, 25), 2, "INFO", "jobs.RatesRefresh", "EJB default - 3", None,
                     "currency rates refreshed: 42 pairs"),
    ]
    for index, op in enumerate(ops):
        sec = _at(12, 30, index * 9)
        thread = f"default task-{7 + index % 6}"
        mdc = f"rqUID={op['rq']}, operUID={op['oper']}"
        lines.append(_payhub_line(sec, 104, "INFO", "transfer.TransferResource", thread, mdc,
                                  f"Transfer request received: amount={op['amount']} RUB, "
                                  f"payer={op['payer']}"))
        outcome = op["outcome"]
        if outcome == "arrest":
            lines.append(_payhub_line(sec + 1, 220, "ERROR", "transfer.TransferProcessor",
                                      thread, mdc, "Transfer rejected by ABS: payer account "
                                      f"{op['payer']} is under arrest (ABS-4013)"))
        elif outcome == "abs-timeout":
            lines.append(_payhub_line(sec + 15, 31, "ERROR", "abs.AbsClient", thread, mdc,
                                      "ABS call createPayment timed out after 15000 ms, "
                                      "operation marked REJECTED"))
        elif outcome == "abs-5001":
            lines.append(_payhub_line(sec + 1, 87, "ERROR", "abs.AbsClient", thread, mdc,
                                      "ABS returned error ABS-5001 for createPayment, operation "
                                      "marked FAILED"))
        elif outcome == "ok":
            lines.append(_payhub_line(sec + 1, 302, "INFO", "transfer.TransferProcessor",
                                      thread, mdc, f"Transfer executed, ABS document "
                                      f"{op['doc']}"))
        elif outcome == "limit":
            lines.append(_payhub_line(sec + 1, 140, "WARN", "transfer.LimitGuard", thread, mdc,
                                      "Daily limit is 90% used for payer "
                                      f"{op['payer']}"))
            lines.append(_payhub_line(sec + 1, 302, "INFO", "transfer.TransferProcessor",
                                      thread, mdc, f"Transfer executed, ABS document "
                                      f"{op['doc']}"))
        if index == 3:
            lines.append(_payhub_line(_at(12, 30, 30), 1, "ERROR", "jobs.Reconciliation",
                                      "EJB default - 5", None, "Reconciliation job failed: SFTP "
                                      "sftp-recon.ift.local:22 auth failed for user recon_uat"))
    return "".join(lines)


def _abs_clef(ops: list[dict[str, Any]]) -> str:
    lines = [_jl({"@t": "2026-10-14T12:20:03.0000000Z", "@mt": "Abs.Adapter {Version} started",
                  "Version": "2.4.0", "SourceContext": "Abs.Adapter.Program"})]
    for index, op in enumerate(ops):
        stamp = f"2026-10-14T{_hms(_at(12, 30, index * 9 + 1))}.{100 + index:03d}0000Z"
        request = f"0HN7B{index:02d}KQ3:00000001"
        if op["outcome"] == "abs-5001":
            lines.append(_jl({"@t": stamp, "@mt": "Limit check failed for {OperUID}: {Reason}",
                              "@l": "Error", "OperUID": op["oper"],
                              "Reason": "daily limit table LIM_DAY_0042 not found",
                              "ErrorCode": "ABS-5001", "RequestId": request,
                              "SourceContext": "Abs.Adapter.Limits"}))
        elif op["outcome"] == "abs-timeout":
            lines.append(_jl({"@t": stamp, "@mt": "Core banking call {Method} for {OperUID} "
                              "took {Elapsed} ms", "@l": "Warning", "Method": "createPayment",
                              "OperUID": op["oper"], "Elapsed": 15020 + index,
                              "RequestId": request, "SourceContext": "Abs.Adapter.Core"}))
        elif op["outcome"] == "arrest":
            lines.append(_jl({"@t": stamp, "@mt": "Payment {OperUID} rejected with {ErrorCode}",
                              "@l": "Warning", "OperUID": op["oper"], "ErrorCode": "ABS-4013",
                              "RequestId": request, "SourceContext": "Abs.Adapter.Payments"}))
        else:
            lines.append(_jl({"@t": stamp, "@mt": "Payment {OperUID} created as {Document}",
                              "OperUID": op["oper"], "Document": op.get("doc", "-"),
                              "RequestId": request, "SourceContext": "Abs.Adapter.Payments"}))
    lines.append(_jl({"@t": "2026-10-14T12:32:00.0000000Z", "@mt": "Health probe {Status}",
                      "Status": "Degraded", "@l": "Warning",
                      "SourceContext": "Abs.Adapter.Health"}))
    return "".join(lines)


def _notify_log(ops: list[tuple[str, str]]) -> str:
    lines = ["[notify-gw] 2026-10-14 12:20:02.004 INFO started notify-gw 0.9.3, providers=[mts, "
             "beeline]\n"]
    for index, (traceparent, outcome) in enumerate(ops):
        stamp = f"2026-10-14 {_hms(_at(12, 31, index * 7))}"
        phone = f"+7900***{1201 + index * 13}"
        lines.append(f"[notify-gw] {stamp}.118 INFO sms queued (traceparent={traceparent} "
                     f"phone={phone})\n")
        if outcome == "sender":
            lines.append(f"[notify-gw] {stamp}.402 ERROR sms send failed: provider responded "
                         "403: sender name FINPLAT is not registered for route mts "
                         f"(traceparent={traceparent} phone={phone})\n")
        elif outcome == "blacklist":
            lines.append(f"[notify-gw] {stamp}.377 ERROR sms send failed: phone {phone} is in "
                         f"provider stop-list (traceparent={traceparent})\n")
        else:
            lines.append(f"[notify-gw] {stamp}.290 INFO sms delivered (traceparent={traceparent} "
                         f"phone={phone})\n")
    lines.append("[notify-gw] 2026-10-14 12:33:00.000 WARN provider beeline latency p99=2.4s\n")
    return "".join(lines)


def operation_logs_v3() -> Case:
    b = LaunchBuilder(8112, "payhub ift regress #8112")

    def op(n: int, outcome: str, amount: str = "1500.00") -> dict[str, Any]:
        return {"rq": _hex(f"rq{n}"), "oper": _hex(f"oper{n}"), "outcome": outcome,
                "amount": amount, "payer": f"408178102000000{4400 + n * 71:05d}",
                "doc": f"0042-{118200 + n}"}

    ops = [op(0, "ok"), op(1, "arrest"), op(2, "abs-timeout"), op(3, "arrest"), op(4, "ok"),
           op(5, "abs-5001"), op(6, "abs-timeout"), op(7, "limit"), op(8, "abs-5001"),
           op(9, "arrest")]
    payhub = _payhub_log(ops)
    abs_log = _abs_clef(ops)
    traces = [f"00-{_hex(f'trace{n}')}-{_hex(f'span{n}', 16)}-01" for n in range(5)]
    notify = _notify_log([(traces[0], "ok"), (traces[1], "sender"), (traces[2], "blacklist"),
                          (traces[3], "sender"), (traces[4], "ok")])

    def json_request(rq: str) -> tuple[str, str, str]:
        return ("request.json", json.dumps({
            "method": "POST", "url": "https://payhub.ift.local/api/v2/transfers",
            "headers": {"rqUID": rq, "Content-Type": "application/json"},
            "body": {"amount": "1500.00", "currency": "RUB"}}, indent=2), "application/json")

    def json_response(o: dict[str, Any], status: str) -> tuple[str, str, str]:
        return ("response.json", json.dumps({
            "status": 200, "headers": {"rqUID": o["rq"]},
            "body": {"operUID": o["oper"], "status": status}}, indent=2), "application/json")

    def xml_request(rq: str) -> tuple[str, str, str]:
        return ("request.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<TransferRq xmlns="urn:finplat:payhub:v2">\n'
            f"  <RqUID>{rq}</RqUID>\n  <RqTm>2026-10-14T12:30:00+03:00</RqTm>\n"
            "  <Amount currency=\"RUB\">1500.00</Amount>\n</TransferRq>\n"), "application/xml")

    rejected = 'expected:<"EXECUTED"> but was:<"REJECTED">'

    def kotest(message: str, spec: str, line: int, extra: str = "") -> str:
        return java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                          [f"ru.finplat.payhub.e2e.{spec}$1$1.invokeSuspend({spec}.kt:{line})",
                           "io.kotest.core.spec.style.scopes.FunSpecRootScope$test$1"
                           ".invokeSuspend(FunSpecRootScope.kt:84)"]) + extra

    def transfer(group: str, cause: str | None, category: str, title: str, message: str,
                 line: int, attachments: list[tuple[str, str, str]],
                 evidence: list[str], with_abs: bool = False) -> None:
        logs = list(attachments)
        if with_abs:
            logs.append(("abs-adapter.clef", abs_log, "text/plain"))
        b.add_failure(group, cause=cause, category=category, name=title,
                      full_name="ru.finplat.payhub.e2e.TransferSpec", message=message,
                      trace=kotest(message, "TransferSpec", line), step="Execute transfer",
                      log=payhub, log_name="payhub.log", logs=logs, evidence=evidence)

    transfer("account-arrested", "payer-account-arrested", "данные", "transfer from salary "
             "account", rejected, 41, [json_request(ops[1]["rq"]),
                                       json_response(ops[1], "REJECTED")],
             [f"payer account {ops[1]['payer']} is under arrest (ABS-4013)"])
    transfer("account-arrested", "payer-account-arrested", "данные", "transfer via SOAP",
             rejected, 58, [xml_request(ops[9]["rq"])],
             [f"payer account {ops[9]['payer']} is under arrest (ABS-4013)"])
    transfer("abs-timeout", "abs-create-payment-timeout", "окружение", "transfer to own card",
             rejected, 73, [json_request(ops[2]["rq"]), json_response(ops[2], "REJECTED")],
             ["ABS call createPayment timed out after 15000 ms"])
    transfer("abs-timeout", "abs-create-payment-timeout", "окружение", "transfer to deposit",
             rejected, 90, [json_response(ops[6], "REJECTED")],
             ["ABS call createPayment timed out after 15000 ms"])
    amount = 'expected:<"1500.00"> but was:<"1500.0">'
    transfer("amount-format", "test-amount-format", "тест", "transfer amount is echoed",
             amount, 104, [json_request(ops[7]["rq"]), json_response(ops[7], "EXECUTED")],
             [amount, f"Transfer executed, ABS document {ops[7]['doc']}"])
    transfer("op-not-in-log", None, "неизвестно", "transfer by phone number", rejected, 121,
             [json_request(_hex("rq-lost")), ("response.json", json.dumps(
                 {"status": 200, "headers": {"rqUID": _hex("rq-lost")},
                  "body": {"status": "REJECTED"}}, indent=2), "application/json")], [])
    failed = 'expected:<"EXECUTED"> but was:<"FAILED">'
    transfer("abs-limit-table", "abs-limit-table-missing", "окружение", "transfer above "
             "100 000", failed, 139, [json_request(ops[5]["rq"])],
             ["daily limit table LIM_DAY_0042 not found"], with_abs=True)
    transfer("abs-limit-table", "abs-limit-table-missing", "окружение", "transfer to "
             "another bank", failed, 152, [json_response(ops[8], "FAILED")],
             ["daily limit table LIM_DAY_0042 not found"], with_abs=True)

    sms = "SMS confirmation code was not delivered within 30 s"
    response = json.dumps({"status": 202, "headers": {"traceparent": traces[1]},
                           "body": {"state": "CODE_SENT"}}, indent=2)
    b.add_failure(
        "sms-sender", cause="sms-sender-not-registered", category="окружение",
        name="confirm transfer by SMS", full_name="ru.finplat.payhub.e2e.ConfirmationSpec",
        message=sms, trace=kotest(sms, "ConfirmationSpec", 37), step="Wait for SMS code",
        log=notify, log_name="notify-gw.log",
        logs=[("response.json", response, "application/json")],
        evidence=["sender name FINPLAT is not registered for route mts"])
    b.add_failure(
        "sms-sender", cause="sms-sender-not-registered", category="окружение",
        name="confirm card binding by SMS", full_name="ru.finplat.payhub.e2e.ConfirmationSpec",
        message=sms,
        trace=kotest(sms, "ConfirmationSpec", 55,
                     "Caused by: ru.finplat.e2e.support.SmsWaitTimeout: no SMS for "
                     f"traceparent={traces[3]} after 30 s\n"
                     "\tat ru.finplat.e2e.support.SmsInbox.await(SmsInbox.kt:48)\n"),
        step="Wait for SMS code", log=notify, log_name="notify-gw.log",
        logs=[("payhub.log", payhub, "text/plain")],
        evidence=["sender name FINPLAT is not registered for route mts"])

    return b.build("operation_logs_v3")


CASES: dict[str, Callable[[], Case]] = {
    "log_formats": log_formats,
    "log_traps": log_traps,
    "same_assertion_db_vs_npe": same_assertion_db_vs_npe,
    "auth_401_403": auth_401_403,
    "timeouts_two_causes": timeouts_two_causes,
    "selenide_pages": selenide_pages,
    "connect_services": connect_services,
    "data_validation": data_validation,
    "no_data": no_data,
    "auth_down_symptoms": auth_down_symptoms,
    "known_issue_symptoms": known_issue_symptoms,
    "retries": retries,
    "retries_by_context": retries_by_context,
    "same_assertion_two_npes": same_assertion_two_npes,
    "step_split_one_cause": step_split_one_cause,
    "unique_conflict_two_objects": unique_conflict_two_objects,
    "log_noise_one_problem": log_noise_one_problem,
    "background_log_errors": background_log_errors,
    "same_exception_two_log_causes": same_exception_two_log_causes,
    "same_exception_split_log_causes": same_exception_split_log_causes,
    "same_assert_http_causes": same_assert_http_causes,
    "resources_split": resources_split,
    "resources_one_problem": resources_one_problem,
    "same_conflict_two_causes": same_conflict_two_causes,
    "mixed_formats": mixed_formats,
    "ui_and_network": ui_and_network,
    "silent_and_step_only": silent_and_step_only,
    "go_log_formats": go_log_formats,
    "java_log_formats": java_log_formats,
    "log_level_controls": log_level_controls,
    "collector_prefixed_logs": collector_prefixed_logs,
    "shared_service_log": shared_service_log,
    "info_business_error": info_business_error,
    "log_formats_more": log_formats_more,
    "log_traps_more": log_traps_more,
    "same_symptom_log_causes": same_symptom_log_causes,
    "hosts_and_locators": hosts_and_locators,
    "shared_assert_background": shared_assert_background,
    "one_problem_noisy": one_problem_noisy,
    "one_problem_resources": one_problem_resources,
    "signatures_and_kb": signatures_and_kb,
    "retries_more": retries_more,
    "sparse_data": sparse_data,
    "log_formats_v3": log_formats_v3,
    "log_traps_v3": log_traps_v3,
    "same_symptom_causes_v3": same_symptom_causes_v3,
    "hosts_locators_v3": hosts_locators_v3,
    "shared_assert_bg_v3": shared_assert_bg_v3,
    "one_problem_noisy_v3": one_problem_noisy_v3,
    "one_problem_resources_v3": one_problem_resources_v3,
    "signatures_kb_v3": signatures_kb_v3,
    "retries_v3": retries_v3,
    "retries_env_v3": retries_env_v3,
    "sparse_data_v3": sparse_data_v3,
    "operation_logs_v3": operation_logs_v3,
    "big_launch": big_launch,
}
