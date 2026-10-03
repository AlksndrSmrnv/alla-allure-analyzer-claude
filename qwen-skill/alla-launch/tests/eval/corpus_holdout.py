"""Отложенный корпус (holdout): другие сервисы, тексты и сочетания форматов.

Правило: пороги, веса и правила подбираются только по dev (``corpus_dev.py``). Holdout
смотрится после, и его базовая линия обновляется отдельным коммитом с объяснением —
иначе он перестаёт проверять, что улучшение не подогнано под dev.
"""

from __future__ import annotations

from collections.abc import Callable

from eval.corpus import Case, LaunchBuilder, java_trace


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


CASES: dict[str, Callable[[], Case]] = {
    "same_conflict_two_causes": same_conflict_two_causes,
    "mixed_formats": mixed_formats,
    "ui_and_network": ui_and_network,
    "silent_and_step_only": silent_and_step_only,
}
