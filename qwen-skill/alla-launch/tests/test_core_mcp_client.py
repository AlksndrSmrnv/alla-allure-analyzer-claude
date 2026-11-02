"""Клиент MCP TestOps: сеанс, транспорт JSON/SSE, повторы, параметры по схеме, полный результат."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import skill_fixtures  # noqa: F401  # добавляет scripts/ в sys.path
from skill_fake_testops import (
    _REAL_ASYNC_CLIENT,
    APP_LOG,
    FIND_TOOL_SCHEMA,
    FULL_EXPAND,
    MCP_SESSION,
    TOKEN,
    FakeTestOps,
    LaunchFixture,
    default_launch,
)

from alla_core.clients.mcp_client import TestOpsMcpClient, _decode
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, ConfigurationError, McpError
from alla_core.services.triage_service import TriageService
from alla_skill_lib.errors import fetch_error_hint

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"endpoint": "https://testops.example", "token": TOKEN, "page_size": 3}
    return Settings(**{**values, **overrides})


def _run(settings: Settings, action: Callable[[TestOpsMcpClient], Any]) -> Any:
    async def main() -> Any:
        async with TestOpsMcpClient(settings) as client:
            client._sleep = _no_sleep  # type: ignore[assignment]
            return await action(client)
    return asyncio.run(main())


SLEPT: list[float] = []


async def _no_sleep(delay: float) -> None:
    SLEPT.append(delay)


@pytest.fixture(autouse=True)
def _clear_sleeps() -> None:
    SLEPT.clear()


def _install(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    class _Client(_REAL_ASYNC_CLIENT):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# --- сеанс и транспорт ---------------------------------------------------------------------


def test_session_headers_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    fake = FakeTestOps(default_launch())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fake.handle(request)

    _install(monkeypatch, handler)
    tools = _run(_settings(), lambda client: client.connect())

    assert [tool["name"] for tool in tools] == ["testops_find_testresults"]
    assert all(request.url == "https://testops.example/api/mcp" for request in seen)
    assert all(request.headers["Authorization"] == f"api-token {TOKEN}" for request in seen)
    methods = [json.loads(request.content)["method"] for request in seen]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert "Mcp-Session-Id" not in seen[0].headers
    assert all(request.headers["Mcp-Session-Id"] == MCP_SESSION for request in seen[1:])
    assert seen[1].headers["MCP-Protocol-Version"] == "2025-06-18"


def test_own_mcp_url_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    fake = FakeTestOps(default_launch())

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return fake.handle(httpx.Request(request.method, "https://testops.example/api/mcp",
                                         headers=request.headers, content=request.content))

    _install(monkeypatch, handler)
    _run(_settings(mcp_url="https://mcp.example/testops"), lambda client: client.connect())
    assert set(urls) == {"https://mcp.example/testops"}


@pytest.mark.parametrize("sse", [False, True])
def test_launch_and_full_result_over_json_and_sse(monkeypatch: pytest.MonkeyPatch, sse: bool) -> None:
    fake = FakeTestOps(default_launch())
    fake.sse = sse
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        results = await client.get_all_test_results_for_launch(777)
        steps = await client.get_test_result_execution(101)
        attachments = await client.get_attachments_for_test_result(101)
        content = await client.get_attachment_content(9001)
        return results, steps, attachments, content

    results, steps, attachments, content = _run(_settings(), action)

    assert [result.id for result in results] == [item["id"] for item in default_launch().results]
    assert steps[0].name == "Создать заказ"
    assert [(item.id, item.name, item.size) for item in attachments] == [
        (9001, "app.log", len(APP_LOG.encode()))]
    assert content == APP_LOG.encode("utf-8")
    # Страницы по 3 и один полный запрос: шаги, вложения и содержимое — из одного ответа.
    assert fake.calls == [
        {"aql": "launch = 777", "page": 0, "size": 3},
        {"aql": "launch = 777", "page": 1, "size": 3},
        {"aql": "launch = 777", "page": 2, "size": 3},
        {"aql": "id = 101", "expand": FULL_EXPAND},
    ]


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_sse_with_a_server_ping_and_any_line_breaks(monkeypatch: pytest.MonkeyPatch,
                                                     newline: str) -> None:
    fake = FakeTestOps(default_launch())
    fake.sse, fake.sse_ping, fake.sse_newline = True, True, newline
    fake.install(monkeypatch)

    page = _run(_settings(), lambda client: client.get_test_results_for_launch(777))
    assert [result.id for result in page.content] == [101, 102, 103]
    # На каждый ping в потоке клиент ответил пустым результатом.
    assert fake.answers and all(answer == {"jsonrpc": "2.0", "id": "srv-1", "result": {}}
                                for answer in fake.answers)


def test_sse_stream_is_closed_after_the_own_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер не закрывает поток после ответа: клиент не ждёт его конца."""
    fake = FakeTestOps(default_launch())

    class _Endless(httpx.AsyncByteStream):
        def __init__(self, first: bytes) -> None:
            self.first = first

        async def __aiter__(self):  # type: ignore[override]
            yield self.first
            await asyncio.sleep(3600)  # поток висит, пока клиент его не закроет
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        message = json.loads(request.content)
        if message.get("method") != "tools/call":
            return response
        frame = b"data: " + response.content + b"\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=_Endless(frame))

    _install(monkeypatch, handler)

    async def action(client: TestOpsMcpClient) -> Any:
        return await asyncio.wait_for(client.get_test_results_for_launch(777), timeout=5)

    page = _run(_settings(), action)
    assert [result.id for result in page.content] == [101, 102, 103]


def test_unknown_protocol_version_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if json.loads(request.content).get("method") == "initialize":
            body = json.loads(response.content)
            body["result"]["protocolVersion"] = "2024-11-05"
            return httpx.Response(200, json=body, headers={"Mcp-Session-Id": MCP_SESSION})
        return response

    _install(monkeypatch, handler)
    with pytest.raises(McpError) as exc_info:
        _run(_settings(), lambda client: client.connect())
    assert exc_info.value.reason == "protocol" and "2024-11-05" in str(exc_info.value)


def test_missing_tool_and_missing_server_get_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    fake.tools = []
    fake.install(monkeypatch)
    settings = _settings()

    with pytest.raises(McpError) as no_tool:
        _run(settings, lambda client: client.connect())
    assert no_tool.value.reason == "no_tool" and "26.2.1.8" in str(no_tool.value)
    assert "ALLURE_MCP_URL" in fetch_error_hint(no_tool.value, settings, Path(".env"))

    for status, reason in ((404, "not_found"), (405, "transport")):
        _install(monkeypatch, lambda request, status=status: httpx.Response(status))
        with pytest.raises(McpError) as exc_info:
            _run(settings, lambda client: client.connect())
        assert exc_info.value.reason == reason
        assert "https://testops.example/api/mcp" in fetch_error_hint(exc_info.value, settings,
                                                                     Path(".env"))


def test_bad_token_is_a_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTestOps(default_launch(), auth_status=403).install(monkeypatch)
    settings = _settings()
    with pytest.raises(AllureApiError) as exc_info:
        _run(settings, lambda client: client.connect())
    assert exc_info.value.status_code == 403
    assert "Проверь ALLURE_TOKEN" in fetch_error_hint(exc_info.value, settings, Path(".env"))


def test_temporary_failures_are_retried_like_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    script: list[object] = [httpx.Response(502), httpx.ConnectError("refused"),
                            httpx.Response(429, headers={"Retry-After": "7"})]

    def handler(request: httpx.Request) -> httpx.Response:
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, httpx.Response)
            return item
        return fake.handle(request)

    _install(monkeypatch, handler)
    _run(_settings(), lambda client: client.connect())
    assert SLEPT == [1.0, 2.0, 7.0]


def test_retries_give_up_and_internal_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def handler(status: int) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(status)
            return httpx.Response(status, text="boom")
        return handle

    _install(monkeypatch, handler(503))
    with pytest.raises(AllureApiError) as exc_info:
        _run(_settings(http_retries=2), lambda client: client.connect())
    assert exc_info.value.status_code == 503 and len(calls) == 3

    calls.clear()
    _install(monkeypatch, handler(500))
    with pytest.raises(AllureApiError):
        _run(_settings(), lambda client: client.connect())
    assert len(calls) == 1


def test_forgotten_session_is_reopened_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    dropped: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        if message.get("method") == "tools/call" and not dropped:
            dropped.append(True)
            return httpx.Response(404)  # сервер перезапустился и забыл сеанс
        return fake.handle(request)

    _install(monkeypatch, handler)
    page = _run(_settings(), lambda client: client.get_test_results_for_launch(777))
    assert [result.id for result in page.content] == [101, 102, 103]
    assert fake.calls == [{"aql": "launch = 777", "page": 0, "size": 3}]


def test_parallel_requests_share_one_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер забыл сеанс посреди параллельной выгрузки: новый сеанс один, и все запросы
    проходят в нём, а не уходят без Mcp-Session-Id."""
    fake = FakeTestOps(default_launch())
    sessions = iter(["session-a", "session-b", "session-c"])
    current: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        if message.get("method") == "initialize":
            current.append(next(sessions))
            body = json.loads(fake.handle(request).content)
            return httpx.Response(200, json=body, headers={"Mcp-Session-Id": current[-1]})
        if request.headers.get("Mcp-Session-Id") != current[-1]:
            return httpx.Response(404)
        headers = dict(request.headers)
        headers["mcp-session-id"] = MCP_SESSION
        forwarded = httpx.Request(request.method, request.url, content=request.content,
                                  headers=headers)
        return fake.handle(forwarded)

    _install(monkeypatch, handler)

    async def action(client: TestOpsMcpClient) -> Any:
        await client.connect()
        current.append("session-z")  # сервер перезапустился: прежний сеанс забыт
        return await asyncio.gather(*(client.get_test_result_execution(result_id)
                                      for result_id in (101, 102, 103, 108)))

    steps = _run(_settings(), action)
    assert len(steps) == 4 and steps[0][0].name == "Создать заказ"
    assert current == ["session-a", "session-z", "session-b"]  # одно новое открытие


def test_tool_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        return await client._call_tool({"aql": "launch = 777", "expand": FULL_EXPAND})

    with pytest.raises(McpError) as exc_info:
        _run(_settings(), action)
    assert exc_info.value.reason == "tool" and "expand работает только с одним id" in str(exc_info.value)


# --- параметры и страницы ------------------------------------------------------------------


def test_parameter_names_come_from_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, Any]] = []
    fake = FakeTestOps(default_launch())
    fake.tools = [{**FIND_TOOL_SCHEMA, "inputSchema": {"type": "object", "properties": {
        "query": {}, "pageNumber": {}, "pageSize": {}, "expansion": {}}}}]
    original = fake._find

    def find(arguments: dict[str, Any]) -> dict[str, Any]:
        received.append(arguments)
        renamed = {"query": "aql", "pageNumber": "page", "pageSize": "size", "expansion": "expand"}
        return original({renamed[key]: value for key, value in arguments.items()})

    fake._find = find  # type: ignore[method-assign]
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        await client.get_test_results_for_launch(777)
        return await client.get_test_result_execution(101)

    _run(_settings(), action)
    assert received == [{"query": "launch = 777", "pageNumber": 0, "pageSize": 3},
                        {"query": "id = 101", "expansion": FULL_EXPAND}]


def test_pages_without_totals_continue_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    original = fake._find

    def find(arguments: dict[str, Any]) -> Any:
        return original(arguments)["content"]  # голый список, без totalPages

    fake._find = find  # type: ignore[method-assign]
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        results = await client.get_all_test_results_for_launch(777)
        return results, client.last_total_elements

    results, total = _run(_settings(), action)
    assert len(results) == 8 and total is None
    assert [call["page"] for call in fake.calls] == [0, 1, 2, 3]  # последняя — пустая


def test_server_page_size_wins_over_the_requested_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер урезает страницу до 2 и не шлёт totalPages: страниц по 2, а не по 3 — все 8."""
    fake = FakeTestOps(default_launch())
    original = fake._find

    def find(arguments: dict[str, Any], with_size: bool) -> Any:
        body = original({**arguments, "size": 2})
        del body["totalPages"]
        if not with_size:
            del body["size"]
        return body

    for with_size in (True, False):
        fake.calls.clear()
        fake._find = lambda arguments, with_size=with_size: find(arguments, with_size)  # type: ignore[method-assign]
        fake.install(monkeypatch)
        results = _run(_settings(), lambda client: client.get_all_test_results_for_launch(777))
        assert len({result.id for result in results}) == len(results) == 8 and [call["page"] for call in fake.calls] == [0, 1, 2, 3]


def test_tool_without_page_parameter_gets_one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    fake.tools = [{**FIND_TOOL_SCHEMA, "inputSchema": {"type": "object", "properties": {
        "aql": {}, "size": {}, "expand": {}}}}]
    fake.install(monkeypatch)

    results = _run(_settings(), lambda client: client.get_all_test_results_for_launch(777))
    assert [result.id for result in results] == [101, 102, 103]  # первая страница — один раз
    assert fake.calls == [{"aql": "launch = 777", "size": 3}]


def test_short_pages_without_totals_continue_until_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер отдаёт по 2 без итогов и размера: короткая страница — ещё не конец."""
    fake = FakeTestOps(default_launch())
    original = fake._find

    def find(arguments: dict[str, Any]) -> Any:
        return original({**arguments, "size": 2})["content"]

    fake._find = find  # type: ignore[method-assign]
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        results = await client.get_all_test_results_for_launch(777)
        return results, client.last_total_elements

    results, total = _run(_settings(), action)
    assert len({result.id for result in results}) == len(results) == 8
    assert total is None  # итог сервер не сообщил — сверять не с чем
    assert [call["page"] for call in fake.calls] == [0, 1, 2, 3, 4]  # последняя — пустая


def test_total_pages_alone_leave_the_result_count_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    original = fake._find

    def find(arguments: dict[str, Any]) -> Any:
        body = original({**arguments, "size": 2})
        return {"content": body["content"], "totalPages": body["totalPages"]}

    fake._find = find  # type: ignore[method-assign]
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        results = await client.get_all_test_results_for_launch(777)
        return results, client.last_total_elements

    results, total = _run(_settings(), action)
    assert len(results) == 8 and total is None  # не 4 × 3 = 11 «насчитанных» TestOps
    assert [call["page"] for call in fake.calls] == [0, 1, 2, 3]


def test_launch_comes_from_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = default_launch()
    fake = FakeTestOps(fixture)
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        return await TriageService(client, _settings()).analyze_launch(777)

    report = _run(_settings(), action)
    assert (report.launch_name, report.project_id, report.fetch.launch_closed) == (
        "Regression nightly", 5, None)
    assert report.launch_created_ms == fixture.launch["createdDate"]  # самый ранний результат
    # Первая страница из get_launch не запрашивается заново.
    assert [call["page"] for call in fake.calls if "page" in call] == [0, 1, 2]


def test_empty_launch_has_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTestOps(default_launch()).install(monkeypatch)
    launch = _run(_settings(), lambda client: client.get_launch(999))
    assert launch.id == 999 and launch.name is None


# --- полный результат ----------------------------------------------------------------------


def _full_fixture(full: dict[str, Any]) -> LaunchFixture:
    fixture = default_launch()
    fixture.details[101] = {"id": 101, "status": "failed", **full}
    return fixture


def test_full_result_decodes_attachments_and_orders_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _full_fixture({
        "message": "expected: <200> but was: <500>",
        "trace": "java.lang.AssertionError",
        "scenario": {"steps": [{"name": "Создать заказ", "status": "failed",
                                "attachments": [{"id": 2}, 3],
                                "steps": [{"name": "вложенный", "status": "passed"}]}]},
        "fixtures": [{"name": "tearDown", "type": "after", "status": "passed"},
                     {"name": "setUp", "type": "before", "status": "passed",
                      "attachments": [1]}],
        "attachments": {
            "1": {"name": "setup.log", "contentType": "text/plain", "contentLength": 5,
                  "content": "ready"},
            "2": {"name": "screen.png", "contentType": "image/png", "contentLength": len(PNG),
                  "content": base64.b64encode(PNG).decode()},
            "3": {"id": 3, "name": "body.json", "contentType": "application/json",
                  "content": '{"a": 1}'},
        },
    })
    FakeTestOps(fixture).install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        steps = await client.get_test_result_execution(101)
        detail = await client.get_test_result_detail(101)
        contents = [await client.get_attachment_content(item) for item in (1, 2, 3)]
        attachments = await client.get_attachments_for_test_result(101)
        return steps, detail, contents, attachments

    steps, detail, contents, attachments = _run(_settings(), action)

    assert [step.name for step in steps] == ["setUp", "Создать заказ", "tearDown"]
    assert [item.id for item in steps[0].attachments or []] == [1]
    body = steps[1]
    assert [item.name for item in body.attachments or []] == ["screen.png", "body.json"]
    assert body.steps is not None and body.steps[0].name == "вложенный"
    assert detail.status_details == {"message": "expected: <200> but was: <500>",
                                     "trace": "java.lang.AssertionError"}
    assert contents == [b"ready", PNG, b'{"a": 1}']
    assert [(item.id, item.size, item.content_type) for item in attachments] == [
        (1, 5, "text/plain"), (2, len(PNG), "image/png"), (3, None, "application/json")]
    assert all("content" not in (item.model_extra or {}) for item in attachments)


def test_named_failed_scenario_root_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _full_fixture({
        "message": "boom",
        "scenario": {"name": "createOrder", "status": "failed",
                     "steps": [{"name": "Открыть", "status": "passed"}]},
        "attachments": {},
    })
    FakeTestOps(fixture).install(monkeypatch)
    steps = _run(_settings(), lambda client: client.get_test_result_execution(101))
    assert [(step.name, step.status) for step in steps] == [("createOrder", "failed")]
    assert steps[0].steps is not None and steps[0].steps[0].name == "Открыть"


def test_message_of_the_full_result_reaches_triage_without_a_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В списке у теста нет ошибки, в полном ответе — только сообщение, шаги passed."""
    fixture = default_launch()
    fixture.details[108] = {"id": 108, "status": "failed", "message": "Order not found",
                            "scenario": {"steps": [{"name": "Шаг", "status": "passed"}]},
                            "attachments": {}}
    FakeTestOps(fixture).install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        return await TriageService(client, _settings()).analyze_launch(777)

    report = _run(_settings(), action)
    silent = next(test for test in report.failed_tests if test.test_result_id == 108)
    assert silent.status_message == "Order not found" and not silent.status_trace


def test_teardown_fixtures_go_after_the_test_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _full_fixture({
        "scenario": {"steps": [{"name": "Тело", "status": "failed"}]},
        "fixtures": {"setups": [{"name": "setUp", "status": "passed"}],
                     "teardowns": [{"name": "tearDown", "status": "broken"}]},
        "attachments": {},
    })
    FakeTestOps(fixture).install(monkeypatch)
    steps = _run(_settings(), lambda client: client.get_test_result_execution(101))
    assert [step.name for step in steps] == ["setUp", "Тело", "tearDown"]


def test_content_is_capped_and_evicted_content_is_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = default_launch()
    fixture.contents[9001] = b"x" * 5000
    fake = FakeTestOps(fixture)
    fake.install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        client.content_budget = 100  # одно вложение больше бюджета — остаётся последним
        await client.get_attachments_for_test_result(101)
        capped = await client.get_attachment_content(9001, max_bytes=1024)
        default = await client.get_attachment_content(9001)
        client._contents.clear()  # вытеснено
        again = await client.get_attachment_content(9001, max_bytes=10)
        return capped, default, again

    capped, default, again = _run(_settings(logs_max_attachment_bytes=2048), action)
    assert (len(capped), len(default), again) == (1024, 2048, b"x" * 10)
    assert [call for call in fake.calls if call.get("expand")] == [
        {"aql": "id = 101", "expand": FULL_EXPAND}] * 2


def test_attachments_of_one_result_larger_than_the_budget_are_all_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = default_launch()
    fixture.attachments[101] = [{"id": 9001 + index, "name": f"part{index}.log", "type": "text/plain"}
                                for index in range(3)]
    for index in range(3):
        fixture.contents[9001 + index] = str(index).encode() * 100
    FakeTestOps(fixture).install(monkeypatch)

    async def action(client: TestOpsMcpClient) -> Any:
        client.content_budget = 150  # помещается одно вложение из трёх
        await client.get_attachments_for_test_result(101)
        return [await client.get_attachment_content(9001 + index) for index in range(3)]

    assert _run(_settings(), action) == [str(index).encode() * 100 for index in range(3)]


def test_detail_without_expand_for_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTestOps(default_launch())
    fake.install(monkeypatch)
    detail = _run(_settings(), lambda client: client.get_test_result_detail(106))
    assert detail.hidden and fake.calls == [{"aql": "id = 106"}]


def test_unknown_attachment_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTestOps(default_launch()).install(monkeypatch)
    with pytest.raises(AllureApiError) as exc_info:
        _run(_settings(), lambda client: client.get_attachment_content(42))
    assert exc_info.value.status_code == 404


@pytest.mark.parametrize(("content", "mime", "expected"), [
    ("test", None, b"test"),  # правильный base64, но без MIME это текст
    (base64.b64encode("лог".encode("cp1251")).decode(), None,
     base64.b64encode("лог".encode("cp1251"))),  # не UTF-8, но и не файл — текст
    ("test", "text/plain", b"test"),
    (base64.b64encode(PNG).decode(), None, PNG),  # нулевые байты — бинарное
    (base64.b64encode(PNG).decode(), "image/png", PNG),
    ("не base64", "image/png", "не base64".encode()),
    ('{"a":1}', "application/vnd.api+json", b'{"a":1}'),
])
def test_attachment_decoding(content: str, mime: str | None, expected: bytes) -> None:
    assert _decode(content, mime) == expected


# --- настройки -----------------------------------------------------------------------------


def test_mcp_url_setting() -> None:
    base = {"ALLURE_ENDPOINT": "https://testops.example/", "ALLURE_TOKEN": "t"}
    assert Settings.load(environ=base).mcp_endpoint == "https://testops.example/api/mcp"
    own = Settings.load(environ={**base, "ALLURE_MCP_URL": "https://mcp.example/x"})
    assert own.mcp_endpoint == "https://mcp.example/x"
    with pytest.raises(ConfigurationError, match="ALLURE_MCP_URL"):
        Settings.load(environ={**base, "ALLURE_MCP_URL": "mcp.example"})
