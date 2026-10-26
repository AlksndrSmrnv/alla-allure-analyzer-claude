"""Синтетический корпус dev: по нему подбираются пороги, веса и правила.

Прогон на сценарий. Тексты — по публичным форматам логов (Spring Boot, logback, log4j2,
JUL, Python logging, logfmt, nginx, Celery, syslog; Go: klog, panic, logrus, zap, zerolog,
slog; JSON-логи Java: logstash-logback-encoder, ECS, JsonTemplateLayout) и по тому, как TestOps отвечает в
``skill_fake_testops.py``. Бывший holdout v2 (launch id 71xx) добавляет тесты Go testify/Gomega,
Jest, WebdriverIO, pytest, NUnit, xUnit, RSpec/Capybara, PHPUnit, Spock и их логи.
"""

from __future__ import annotations

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
    "big_launch": big_launch,
}
