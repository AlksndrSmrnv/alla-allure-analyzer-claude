"""Синтетический корпус dev: по нему подбираются пороги, веса и правила.

Прогон на сценарий. Тексты — по публичным форматам логов (Spring Boot, logback, log4j2,
JUL, Python logging, logfmt, nginx) и по тому, как TestOps отвечает в
``skill_fake_testops.py``.
"""

from __future__ import annotations

from collections.abc import Callable

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
    "same_assertion_two_npes": same_assertion_two_npes,
    "log_noise_one_problem": log_noise_one_problem,
    "background_log_errors": background_log_errors,
    "resources_split": resources_split,
    "resources_one_problem": resources_one_problem,
    "big_launch": big_launch,
}
