"""Клиент встроенного MCP-сервера Allure TestOps (``<endpoint>/api/mcp``).

REST API TestOps у пользователей скилла закрыт, открыт только MCP. Нужен один инструмент —
``testops_find_testresults`` (поиск результатов по AQL):

- список результатов прогона — ``aql: "launch = <id>"`` со страницами, без expand;
- результат целиком — ``aql: "id = <id>"`` (строго один id: со списком expand не работает),
  ``expand: ["scenario", "fixtures", "attachments"]``. В ответе дерево шагов (``scenario``),
  фикстуры и словарь вложений ``{"<id>": {..., "content", "contentLength"}}``: текст —
  как есть, бинарные (скриншоты) — base64 в том же ``content``.

Транспорт — Streamable HTTP поверх ``httpx`` (JSON-RPC в POST, ответ JSON или SSE): пакет
``mcp`` тянет веб-сервер и десяток зависимостей ради трёх методов. Все вызовы только читают,
поэтому повторяются при временных сбоях, как GET у прежнего клиента (``clients/retry.py``).

Один запрос с полным expand на результат обслуживает дерево шагов, ``trace``, список
вложений и их содержимое (протоколы ``TestResultsProvider`` и ``AttachmentProvider``):
сервисы ядра работают как с REST. Содержимое вложений держится в памяти в пределах
бюджета; вытесненное запрашивается заново тем же запросом результата.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from alla_core.clients.retry import RETRY_STATUSES, header, retry_delay
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, McpError, PaginationLimitError
from alla_core.models.common import PageResponse
from alla_core.models.testops import AttachmentMeta, ExecutionStep, LaunchResponse, TestResultResponse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
FIND_RESULTS_TOOL = "testops_find_testresults"
FULL_EXPAND = ("scenario", "fixtures", "attachments")
# Сколько байт содержимого вложений держать между запросом результата и разбором логов.
CONTENT_BUDGET_BYTES = 128 * 1024 * 1024
_ENDPOINT = f"mcp:{FIND_RESULTS_TOOL}"
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-ndjson")
# Сигнатуры PNG, JPEG, GIF, WebP/RIFF, PDF, ZIP — бинарное вложение без MIME.
_BINARY_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"%PDF", b"PK\x03\x04")

# Имена параметров инструмента не опубликованы: берём из inputSchema первое совпадение.
_PARAM_CANDIDATES = {
    "aql": ("aql", "query", "rql", "filter"),
    "page": ("page", "pageNumber"),
    "size": ("size", "pageSize", "limit"),
    "expand": ("expand", "expansion", "expands"),
}


@dataclass
class _FullResult:
    """Результат с полным expand: модель, дерево шагов, метаданные вложений."""

    result: TestResultResponse
    steps: list[ExecutionStep]
    attachments: list[AttachmentMeta]


class TestOpsMcpClient:
    """Чтение прогона TestOps через MCP; протоколы ``TestResultsProvider`` и ``AttachmentProvider``."""

    __test__ = False  # имя начинается с Test: pytest не должен собирать класс

    def __init__(self, settings: Settings) -> None:
        self._url = settings.mcp_endpoint
        self._token = settings.token
        self._page_size = settings.page_size
        self._max_pages = settings.max_pages
        self._max_attachment_bytes = settings.logs_max_attachment_bytes
        # Хранится не больше, чем попросит самый щедрый потребитель (логи или скриншоты).
        self._stored_bytes = max(settings.logs_max_attachment_bytes, settings.images_max_bytes + 1)
        self._retries = settings.http_retries
        self.last_total_elements: int | None = None
        self._sleep = asyncio.sleep  # подменяется в тестах
        self._http = httpx.AsyncClient(timeout=settings.request_timeout)
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._params: dict[str, str | None] | None = None
        self._init_lock = asyncio.Lock()
        self._rpc_id = 0
        self._first_pages: dict[int, PageResponse[TestResultResponse]] = {}
        self._full: dict[int, _FullResult] = {}
        self._full_locks: dict[int, asyncio.Lock] = {}
        self._owner: dict[int, int] = {}  # id вложения → id результата
        self._contents: OrderedDict[int, bytes] = OrderedDict()
        self._contents_size = 0
        self.content_budget = CONTENT_BUDGET_BYTES

    # --- Подключение ---

    async def connect(self) -> list[dict[str, Any]]:
        """Открыть сеанс MCP и вернуть инструменты сервера; нет нужного — ``McpError(no_tool)``."""
        async with self._init_lock:
            return await self._initialize()

    async def _initialize(self) -> list[dict[str, Any]]:
        self._session_id = None
        self._protocol = None
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alla-launch", "version": "1"},
        }, initializing=True)
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        self._protocol = version if isinstance(version, str) else PROTOCOL_VERSION
        await self._notify("notifications/initialized")
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            listed = await self._rpc("tools/list", {"cursor": cursor} if cursor else {},
                                     initializing=True)
            page = listed.get("tools") if isinstance(listed, dict) else None
            tools.extend(item for item in page or [] if isinstance(item, dict))
            cursor = listed.get("nextCursor") if isinstance(listed, dict) else None
            if not cursor:
                break
        tool = next((item for item in tools if item.get("name") == FIND_RESULTS_TOOL), None)
        if tool is None:
            raise McpError(
                "no_tool",
                f"на сервере нет инструмента {FIND_RESULTS_TOOL} — нужен TestOps 26.2.1.8 "
                "или новее с включённым MCP",
            )
        self._params = _tool_params(tool)
        return tools

    async def _ensure_ready(self) -> dict[str, str | None]:
        if self._params is None:
            async with self._init_lock:
                if self._params is None:
                    await self._initialize()
        assert self._params is not None
        return self._params

    # --- Протокол TestResultsProvider ---

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Метаданные прогона из первой страницы его результатов: инструмента прогона в MCP нет.

        Имя и проект — если сервер прислал их в результатах; ``closed`` и время прогона
        неизвестны (``None``: триаж возьмёт время самого раннего результата). Несуществующий
        и пустой прогон по MCP не различить: оба — прогон без результатов (``prepare`` так и
        скажет).
        """
        page = await self.get_test_results_for_launch(launch_id)
        self._first_pages[launch_id] = page
        first = page.content[0] if page.content else None
        extra = (first.model_extra or {}) if first is not None else {}
        launch_raw = extra.get("launch")
        launch: dict[str, Any] = launch_raw if isinstance(launch_raw, dict) else {}
        name = extra.get("launchName") or launch.get("name")
        project = extra.get("projectId") or launch.get("projectId")
        closed = launch.get("closed")
        return LaunchResponse.model_validate({
            "id": launch_id,
            "name": name if isinstance(name, str) else None,
            "projectId": project if isinstance(project, int) else None,
            "closed": closed if isinstance(closed, bool) else None,
        })

    async def get_test_results_for_launch(
        self,
        launch_id: int,
        page: int = 0,
        size: int | None = None,
    ) -> PageResponse[TestResultResponse]:
        """Одна страница результатов прогона: ``aql: "launch = <id>"``."""
        size = size or self._page_size
        params = await self._ensure_ready()
        arguments: dict[str, Any] = {params["aql"] or "aql": f"launch = {launch_id}"}
        if params["page"]:
            arguments[params["page"]] = page
        if params["size"]:
            arguments[params["size"]] = size
        payload = await self._call_tool(arguments)
        return _page(payload, page, size, paged=bool(params["page"]))

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Все результаты прогона, включая hidden, по страницам; потолок — ``max_pages``."""
        all_results: list[TestResultResponse] = []
        page = 0
        while True:
            cached = self._first_pages.pop(launch_id, None) if page == 0 else None
            page_resp = cached or await self.get_test_results_for_launch(
                launch_id, page=page, size=self._page_size,
            )
            all_results.extend(page_resp.content)
            self.last_total_elements = page_resp.total_elements
            if page == 0 and page_resp.total_pages > self._max_pages:
                raise PaginationLimitError(
                    f"В прогоне {page_resp.total_elements} результатов "
                    f"({page_resp.total_pages} страниц по {self._page_size}), "
                    f"лимит — {self._max_pages} страниц.",
                    pages_needed=page_resp.total_pages,
                )
            if page + 1 >= page_resp.total_pages:
                break
            page += 1
            if page >= self._max_pages:
                raise PaginationLimitError(
                    f"Превышен max_pages={self._max_pages}. "
                    f"Получено {len(all_results)}/{page_resp.total_elements} "
                    f"результатов. Увеличьте ALLURE_MAX_PAGES при необходимости.",
                    pages_needed=page_resp.total_pages,
                )
        logger.info("Получено %d результатов тестов для запуска %d (%d страниц)",
                    len(all_results), launch_id, page + 1)
        return all_results

    async def get_test_result_detail(self, test_result_id: int) -> TestResultResponse:
        """Результат по id (``trace`` и ``statusDetails``): из полного запроса, иначе без expand."""
        full = self._full.get(test_result_id)
        if full is not None:
            return full.result
        return _result(await self._find_one(test_result_id, expand=False))

    async def get_test_result_execution(self, test_result_id: int) -> list[ExecutionStep]:
        """Дерево шагов: ``scenario`` и фикстуры (before — в начале, after — в конце)."""
        return (await self._full_result(test_result_id)).steps

    # --- Протокол AttachmentProvider ---

    async def get_attachments_for_test_result(self, test_result_id: int) -> list[AttachmentMeta]:
        """Метаданные вложений результата (``size`` — ``contentLength``, размер исходника)."""
        return (await self._full_result(test_result_id)).attachments

    async def get_attachment_content(
        self, attachment_id: int, *, max_bytes: int | None = None,
    ) -> bytes:
        """Содержимое вложения, обрезанное до ``max_bytes`` (по умолчанию ``logs_max_attachment_bytes``).

        Вложение известно только после запроса его результата; вытесненное из памяти
        запрашивается заново тем же запросом.
        """
        cap = self._max_attachment_bytes if max_bytes is None else max_bytes
        data = self._contents.get(attachment_id)
        if data is None:
            owner = self._owner.get(attachment_id)
            if owner is None:
                raise AllureApiError(404, f"вложение {attachment_id} не из запрошенных результатов",
                                     _ENDPOINT)
            await self._full_result(owner, refresh=True)
            data = self._contents.get(attachment_id)
            if data is None:
                raise AllureApiError(404, f"вложение {attachment_id} не пришло в ответе", _ENDPOINT)
        else:
            self._contents.move_to_end(attachment_id)
        if len(data) > cap:
            logger.warning("Вложение %d обрезано: %d байт, лимит %d", attachment_id, len(data), cap)
        return data[:cap]

    # --- Полный результат ---

    async def _full_result(self, test_result_id: int, *, refresh: bool = False) -> _FullResult:
        lock = self._full_locks.setdefault(test_result_id, asyncio.Lock())
        async with lock:
            cached = self._full.get(test_result_id)
            if cached is not None and not refresh:
                return cached
            raw = await self._find_one(test_result_id, expand=True)
            full = self._parse_full(test_result_id, raw)
            self._full[test_result_id] = full
            return full

    async def _find_one(self, test_result_id: int, *, expand: bool) -> dict[str, Any]:
        params = await self._ensure_ready()
        arguments: dict[str, Any] = {params["aql"] or "aql": f"id = {test_result_id}"}
        if expand:
            arguments[params["expand"] or "expand"] = list(FULL_EXPAND)
        page = _items(await self._call_tool(arguments))
        item = next((entry for entry in page if entry.get("id") == test_result_id), None)
        if item is None and len(page) == 1:
            item = page[0]
        if item is None:
            raise AllureApiError(404, f"результат {test_result_id} не найден", _ENDPOINT)
        return item

    def _parse_full(self, test_result_id: int, raw: dict[str, Any]) -> _FullResult:
        metas: dict[int, AttachmentMeta] = {}
        attachments = raw.get("attachments")
        entries = (
            [(key, value) for key, value in attachments.items()] if isinstance(attachments, dict)
            else [(None, value) for value in attachments] if isinstance(attachments, list)
            else []
        )
        for key, value in entries:
            meta = self._store_attachment(test_result_id, key, value)
            if meta is not None and meta.id is not None:
                metas[meta.id] = meta
        steps = [_step(item, metas) for item in _scenario_steps(raw.get("scenario"))]
        before, after = _fixtures(raw.get("fixtures"))
        steps = ([_step(item, metas) for item in before] + steps
                 + [_step(item, metas) for item in after])
        body = {key: value for key, value in raw.items()
                if key not in ("scenario", "fixtures", "attachments")}
        return _FullResult(_result(body), steps, list(metas.values()))

    def _store_attachment(self, test_result_id: int, key: object, value: object) -> AttachmentMeta | None:
        if not isinstance(value, dict):
            return None
        attachment_id = _int(value.get("id"))
        if attachment_id is None:
            attachment_id = _int(key)
        if attachment_id is None:
            return None
        fields = {name: item for name, item in value.items() if name != "content"}
        fields["id"] = attachment_id
        if fields.get("size") is None and _int(value.get("contentLength")) is not None:
            fields["size"] = _int(value.get("contentLength"))
        meta = AttachmentMeta.model_validate(fields)
        self._owner[attachment_id] = test_result_id
        content = value.get("content")
        if isinstance(content, str):
            data = _decode(content, meta.content_type or meta.type)[:self._stored_bytes]
            self._remember_content(attachment_id, data)
        return meta

    def _remember_content(self, attachment_id: int, data: bytes) -> None:
        old = self._contents.pop(attachment_id, None)
        if old is not None:
            self._contents_size -= len(old)
        self._contents[attachment_id] = data
        self._contents_size += len(data)
        while self._contents_size > self.content_budget and len(self._contents) > 1:
            _, evicted = self._contents.popitem(last=False)
            self._contents_size -= len(evicted)

    # --- JSON-RPC ---

    async def _call_tool(self, arguments: dict[str, Any]) -> Any:
        result = await self._rpc("tools/call", {"name": FIND_RESULTS_TOOL, "arguments": arguments})
        return tool_payload(result, f"{FIND_RESULTS_TOOL} ({arguments})")

    async def _rpc(self, method: str, params: dict[str, Any], *, initializing: bool = False) -> Any:
        """Запрос JSON-RPC; на 404 с сеансом (сервер его забыл) — один новый сеанс."""
        if not initializing:
            await self._ensure_ready()
        self._rpc_id += 1
        request_id = self._rpc_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        resp = await self._post(message)
        if resp.status_code == 404 and self._session_id and not initializing:
            logger.info("MCP TestOps: сеанс истёк, открываю новый")
            async with self._init_lock:
                await self._initialize()
            resp = await self._post(message)
        self._check_status(resp)
        if initializing and method == "initialize":
            session = header(resp, "Mcp-Session-Id")
            self._session_id = session if isinstance(session, str) and session else None
        reply = rpc_reply(resp, request_id)
        error = reply.get("error")
        if isinstance(error, dict):
            raise McpError("tool", f"{method}: {error.get('message') or error}")
        return reply.get("result")

    async def _notify(self, method: str) -> None:
        resp = await self._post({"jsonrpc": "2.0", "method": method})
        self._check_status(resp)

    def _check_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status < 400:
            return
        if status == 404:
            raise McpError("not_found", f"по адресу {self._url} нет MCP-сервера (HTTP 404)")
        if status == 405:
            raise McpError(
                "transport",
                f"{self._url} не принимает Streamable HTTP (HTTP 405): скилл не умеет старый "
                "SSE-транспорт MCP",
            )
        raise AllureApiError(status, resp.text[:500], "mcp")

    async def _post(self, message: dict[str, Any]) -> httpx.Response:
        """POST с повторами: все вызовы читают, поэтому повторяются как GET прежнего клиента."""
        headers = {
            "Authorization": f"api-token {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        method = str(message.get("method"))
        attempt = 0
        while True:
            try:
                resp = await self._http.post(self._url, json=message, headers=headers)
            except httpx.RequestError as exc:
                if attempt >= self._retries:
                    raise AllureApiError(0, str(exc), "mcp") from exc
                await self._pause(method, attempt, "сбой сети", None)
            else:
                if resp.status_code not in RETRY_STATUSES or attempt >= self._retries:
                    return resp
                await self._pause(method, attempt, f"HTTP {resp.status_code}",
                                  header(resp, "Retry-After"))
            attempt += 1

    async def _pause(self, method: str, attempt: int, reason: str, retry_after: object) -> None:
        delay = retry_delay(retry_after, attempt)
        logger.warning("TestOps MCP: %s — %s, повтор %d из %d через %.0f с",
                       method, reason, attempt + 1, self._retries, delay)
        await self._sleep(delay)

    # --- Жизненный цикл ---

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> TestOpsMcpClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


def _tool_params(tool: dict[str, Any]) -> dict[str, str | None]:
    """Имена параметров инструмента по его ``inputSchema``; схемы нет — имена по умолчанию."""
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return {"aql": "aql", "page": "page", "size": "size", "expand": "expand"}
    return {
        role: next((name for name in names if name in properties), None)
        for role, names in _PARAM_CANDIDATES.items()
    }


def rpc_reply(resp: httpx.Response, request_id: object) -> dict[str, Any]:
    """Ответ на запрос ``request_id`` из тела JSON или потока SSE."""
    content_type = (resp.headers.get("content-type") or "").lower()
    messages: list[Any] = []
    if content_type.startswith("text/event-stream"):
        for event in resp.text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.split("\n")
                             if line.startswith("data:"))
            if data:
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            body = resp.json()
        except ValueError:
            raise McpError("protocol", f"ответ не JSON: {resp.text[:200]}") from None
        messages = body if isinstance(body, list) else [body]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpError("protocol", f"в ответе нет сообщения с id {request_id}")


def tool_payload(result: object, label: str = FIND_RESULTS_TOOL) -> Any:
    """Данные из результата ``tools/call``: ``structuredContent``, иначе JSON текста.

    ``isError`` — ``McpError(tool)`` с текстом сервера, не JSON — ``McpError(protocol)``.
    """
    if not isinstance(result, dict):
        raise McpError("protocol", f"{label}: ответ без result")
    text = "\n".join(
        str(item.get("text", "")) for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    )
    if result.get("isError"):
        raise McpError("tool", f"{label}: {text[:500] or 'ошибка'}")
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    try:
        return json.loads(text)
    except ValueError:
        raise McpError("protocol", f"{label}: ответ не JSON: {text[:200]}") from None


def tool_items(result: object) -> list[dict[str, Any]]:
    """Результаты тестов из ответа ``tools/call`` (страница, список или один объект)."""
    return _items(tool_payload(result))


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("content", "items", "results", "data"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        if "id" in payload:
            return [payload]
    return []


def _page(payload: Any, page: int, size: int, *, paged: bool) -> PageResponse[TestResultResponse]:
    """Страница результатов в ``PageResponse``; итогов нет — ещё страница, пока эта полная."""
    items = [_result(item) for item in _items(payload)]
    meta = payload if isinstance(payload, dict) else {}
    total = _int(meta.get("totalElements", meta.get("total")))
    pages = _int(meta.get("totalPages"))
    if pages is None and total is not None:
        pages = max(1, math.ceil(total / size))
    if pages is None:
        pages = page + 2 if paged and len(items) >= size else page + 1
    return PageResponse[TestResultResponse](
        content=items,
        totalElements=total if total is not None else page * size + len(items),
        totalPages=pages,
        size=size,
        number=page,
    )


def _result(raw: dict[str, Any]) -> TestResultResponse:
    """Модель результата; ``message``/``trace`` верхнего уровня — в ``statusDetails``, если его нет."""
    data = dict(raw)
    if not isinstance(data.get("statusDetails"), dict):
        details = {key: data[key] for key in ("message", "trace") if isinstance(data.get(key), str)}
        if details:
            data["statusDetails"] = details
    return TestResultResponse.model_validate(data)


def _scenario_steps(scenario: object) -> list[dict[str, Any]]:
    """Корни дерева шагов: список, ``{steps: [...]}`` (обёртка с ошибкой — сама корень), ``{content}``."""
    if isinstance(scenario, list):
        return [item for item in scenario if isinstance(item, dict)]
    if not isinstance(scenario, dict):
        return []
    if any(scenario.get(key) for key in ("statusDetails", "message", "trace")):
        return [scenario]
    for key in ("steps", "content", "children"):
        if isinstance(scenario.get(key), list):
            return [item for item in scenario[key] if isinstance(item, dict)]
    return [scenario] if scenario.get("name") else []


def _fixtures(fixtures: object) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Фикстуры: ``{befores, afters}`` или список с ``type`` (``after…`` — после теста)."""
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    if isinstance(fixtures, dict):
        for key, value in fixtures.items():
            if isinstance(value, list):
                target = after if "after" in str(key).lower() else before
                target.extend(item for item in value if isinstance(item, dict))
    elif isinstance(fixtures, list):
        for item in fixtures:
            if isinstance(item, dict):
                kind = str(item.get("type") or item.get("phase") or "").lower()
                (after if "after" in kind or "teardown" in kind else before).append(item)
    return before, after


def _step(raw: dict[str, Any], metas: dict[int, AttachmentMeta]) -> ExecutionStep:
    """Шаг с подшагами; вложения шага (id или объект) — метаданными из словаря вложений."""
    data = dict(raw)
    children = data.get("steps")
    if not isinstance(children, list):
        children = data.get("children") if isinstance(data.get("children"), list) else None
    data["steps"] = [_step(item, metas) for item in children or [] if isinstance(item, dict)] \
        if children is not None else None
    data.pop("children", None)
    refs = data.get("attachments")
    if isinstance(refs, list):
        attachments: list[AttachmentMeta] = []
        for ref in refs:
            attachment_id = _int(ref.get("id")) if isinstance(ref, dict) else _int(ref)
            if attachment_id is not None and attachment_id in metas:
                attachments.append(metas[attachment_id])
            elif isinstance(ref, dict):
                attachments.append(AttachmentMeta.model_validate(
                    {key: value for key, value in ref.items() if key != "content"}))
        data["attachments"] = attachments
    else:
        data.pop("attachments", None)
    return ExecutionStep.model_validate(data)


def _decode(content: str, mime: str | None) -> bytes:
    """Байты вложения: текстовый MIME — строка как есть (UTF-8), другой MIME — base64.

    Без MIME строка — base64, только если раскодированное похоже на бинарный файл (сигнатура
    картинки, PDF, архива или нулевые байты): слово «test» тоже правильный base64, а лог
    без типа должен остаться логом.
    """
    kind = (mime or "").lower().split(";")[0].strip()
    text = content.encode("utf-8")
    if kind.startswith(_TEXT_TYPES) or kind.endswith(("+json", "+xml")):
        return text
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return text
    if kind or data.startswith(_BINARY_MAGIC) or b"\x00" in data[:4096]:
        return data
    return text


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
