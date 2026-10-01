"""Фейковый Allure TestOps на ``httpx.MockTransport``.

Подменяется ``httpx.AsyncClient``, поэтому вендоренные auth, пагинация,
fallback-запросы и стриминг вложений работают как с настоящим сервером.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

_REAL_ASYNC_CLIENT = httpx.AsyncClient
TOKEN = "secret-api-token-4242"

ORDER_TRACE = (
    "java.lang.AssertionError: expected: <200> but was: <500>\n"
    "\tat org.junit.Assert.fail(Assert.java:89)\n"
    "\tat org.junit.Assert.assertEquals(Assert.java:120)\n"
    "\tat ru.company.orders.OrderTest.{method}(OrderTest.java:{line})\n"
    "\tat java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)\n"
)
APP_LOG = (
    "2026-09-01 10:00:00 [INFO] OrderService: request received\n"
    "2026-09-01 10:00:01 [ERROR] OrderService: failed to create order\n"
    "java.lang.NullPointerException: customer is null\n"
    "\tat ru.company.OrderService.create(OrderService.java:10)\n"
    "2026-09-01 10:00:02 [INFO] OrderService: request finished\n"
)


@dataclass
class LaunchFixture:
    launch: dict[str, Any]
    results: list[dict[str, Any]]
    executions: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    details: dict[int, dict[str, Any]] = field(default_factory=dict)
    attachments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    contents: dict[int, bytes] = field(default_factory=dict)


def _order_result(result_id: int, method: str, line: int) -> dict[str, Any]:
    return {
        "id": result_id,
        "name": method,
        "fullName": f"ru.company.orders.OrderTest.{method}",
        "status": "failed",
        "statusDetails": {
            "message": "expected: <200> but was: <500>",
            "trace": ORDER_TRACE.format(method=method, line=line),
        },
    }


def default_launch(launch_id: int = 777) -> LaunchFixture:
    """Прогон: 2 одинаковых падения, 1 broken, 1 без данных, muted, hidden, passed, skipped.

    Другой ``launch_id`` — «следующий прогон» с теми же падениями.
    """
    results = [
        _order_result(101, "createOrder", 6),
        _order_result(102, "updateOrder", 11),
        {
            "id": 103,
            "name": "login",
            "fullName": "ru.company.auth.LoginTest.login",
            "status": "broken",
            "statusDetails": {
                "message": "java.net.ConnectException: Connection refused: auth-service:8080",
                "trace": "java.net.ConnectException: Connection refused\n"
                "\tat ru.company.auth.LoginTest.login(LoginTest.java:4)\n",
            },
        },
        {"id": 104, "name": "search", "status": "passed"},
        {"id": 105, "name": "flaky", "status": "failed", "muted": True,
         "statusDetails": {"message": "muted failure"}},
        {"id": 106, "name": "createOrder retry", "status": "failed", "hidden": True,
         "statusDetails": {"message": "retry"}},
        {"id": 107, "name": "export", "status": "skipped"},
        {"id": 108, "name": "silent", "fullName": "ru.company.misc.SilentTest.silent",
         "status": "failed"},
    ]
    step = {
        "name": "Создать заказ",
        "status": "failed",
        "statusDetails": {"message": "expected: <200> but was: <500>"},
    }
    return LaunchFixture(
        launch={"id": launch_id, "name": "Regression nightly", "projectId": 5},
        results=results,
        executions={101: [step], 102: [step]},
        details={108: {"id": 108, "name": "silent", "status": "failed"}},
        attachments={101: [{"id": 9001, "name": "app.log", "type": "text/plain"}]},
        contents={9001: APP_LOG.encode("utf-8")},
    )


_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
    "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
    "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu",
)


def many_launch(count: int, launch_id: int = 900) -> LaunchFixture:
    """Прогон из ``count`` падений, каждое — свой кластер (у всех разные шаг и сообщение)."""
    results: list[dict[str, Any]] = []
    executions: dict[int, list[dict[str, Any]]] = {}
    for index in range(count):
        word = _WORDS[index % len(_WORDS)] + _WORDS[(index // len(_WORDS)) % len(_WORDS)]
        result_id = 1000 + index
        message = f"{word} service returned {index} unexpected records for {word} account"
        results.append({
            "id": result_id,
            "name": f"check_{word}",
            "fullName": f"ru.company.misc.{word.title()}Test.check_{word}",
            "status": "failed",
            "statusDetails": {"message": message, "trace": f"java.lang.IllegalStateException: {message}\n"},
        })
        executions[result_id] = [{
            "name": f"Step {word} {'x' * (index % 7)} {index * 37}",
            "status": "failed",
            "statusDetails": {"message": message},
        }]
    return LaunchFixture(
        launch={"id": launch_id, "name": "Big regression", "projectId": 5},
        results=results,
        executions=executions,
    )


def green_launch() -> LaunchFixture:
    return LaunchFixture(
        launch={"id": 778, "name": "Smoke", "projectId": 5},
        results=[
            {"id": 201, "name": "a", "status": "passed"},
            {"id": 202, "name": "b", "status": "passed"},
        ],
    )


class FakeTestOps:
    """Обработчик запросов TestOps с журналом (метод, путь)."""

    def __init__(self, fixture: LaunchFixture, *, auth_status: int = 200) -> None:
        self.fixture = fixture
        self.auth_status = auth_status
        self.requests: list[tuple[str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class _Client(_REAL_ASYNC_CLIENT):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(fake.handle)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        fixture = self.fixture
        if path == "/api/uaa/oauth/token":
            if self.auth_status != 200:
                return httpx.Response(self.auth_status, json={"error": "unauthorized"})
            return httpx.Response(200, json={"access_token": "jwt-1", "expires_in": 3600})
        if path == f"/api/launch/{fixture.launch['id']}":
            return httpx.Response(200, json=fixture.launch)
        if path == "/api/testresult":
            return self._page(request)
        if path == "/api/testresult/attachment":
            test_id = int(request.url.params["testResultId"])
            return httpx.Response(200, json={"content": fixture.attachments.get(test_id, [])})
        if path.startswith("/api/testresult/attachment/") and path.endswith("/content"):
            attachment_id = int(path.split("/")[-2])
            return httpx.Response(200, content=fixture.contents[attachment_id])
        if path.startswith("/api/testresult/") and path.endswith("/execution"):
            test_id = int(path.split("/")[-2])
            return httpx.Response(200, json=fixture.executions.get(test_id, []))
        if path.startswith("/api/testresult/"):
            test_id = int(path.split("/")[-1])
            return httpx.Response(200, json=fixture.details.get(test_id, {"id": test_id}))
        return httpx.Response(404, text=json.dumps({"path": path}))

    def _page(self, request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        results = self.fixture.results
        return httpx.Response(200, json={
            "content": results[page * size:(page + 1) * size],
            "totalElements": len(results),
            "totalPages": max(1, math.ceil(len(results) / size)),
            "size": size,
            "number": page,
        })
