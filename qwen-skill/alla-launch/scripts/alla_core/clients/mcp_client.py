"""Клиент встроенного MCP-сервера Allure TestOps (``<endpoint>/api/mcp``).

REST API TestOps у пользователей скилла закрыт, открыт только MCP. Нужен один инструмент —
``testops_find_testresults`` (поиск результатов по AQL в проекте ``projectId``: номер прогона
проекта не называет, проект — из ``ALLURE_PROJECT_ID``, выбор модуля — в ``prepare``):

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

from alla_core.clients.retry import RETRY_STATUSES, retry_delay
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, McpError, PaginationLimitError
from alla_core.models.common import PageResponse
from alla_core.models.testops import AttachmentMeta, ExecutionStep, LaunchResponse, TestResultResponse
from alla_core.origins import require_allowed_origin

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
# Версии Streamable HTTP: 2024-11-05 — старый SSE-транспорт, его клиент не умеет.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
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
    "project": ("projectId", "project_id", "project"),
}


@dataclass
class _Exchange:
    """Ответ сервера на один POST: статус, заголовки, тело (не SSE) и свой ответ JSON-RPC."""

    status: int
    headers: httpx.Headers
    text: str
    reply: dict[str, Any] | None


@dataclass
class _FullResult:
    """Результат с полным expand: модель, дерево шагов, метаданные вложений."""

    result: TestResultResponse
    steps: list[ExecutionStep]
    attachments: list[AttachmentMeta]


class TestOpsMcpClient:
    """Чтение прогона TestOps через MCP; протоколы ``TestResultsProvider`` и ``AttachmentProvider``."""

    __test__ = False  # имя начинается с Test: pytest не должен собирать класс

    def __init__(self, settings: Settings, *, project_id: int | None = None) -> None:
        settings.validate_testops_origins()
        self._allowed_origins = settings.allowed_origins
        self._url = settings.mcp_endpoint
        # Проект TestOps всех запросов (``projectId``); ``None`` — не слать.
        self.project_id = project_id
        # Схема инструмента требует проект (известно после ``connect``).
        self.project_required = False
        # В схеме инструмента есть параметр проекта (известно после ``connect``).
        self.project_param = False
        self._token = settings.token
        self._page_size = settings.page_size
        self._max_pages = settings.max_pages
        self._max_attachment_bytes = settings.logs_max_attachment_bytes
        # Хранится не больше, чем попросит самый щедрый потребитель (логи или скриншоты).
        self._stored_bytes = max(settings.logs_max_attachment_bytes, settings.images_max_bytes + 1)
        self._retries = settings.http_retries
        self.last_total_elements: int | None = None
        self._sleep = asyncio.sleep  # подменяется в тестах
        self._http = httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False)
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._params: dict[str, str | None] | None = None
        self._init_lock = asyncio.Lock()
        self._rpc_id = 0
        self._first_pages: dict[int, tuple[PageResponse[TestResultResponse], bool, bool]] = {}
        self._server_page_size: int | None = None
        self._totals_known = True  # сервер сообщил totalPages или число результатов
        self._total_known = True  # сервер сообщил число результатов
        self._generation = 0
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
        """Новый сеанс: ``initialize`` без старого сеанса, затем ``tools/list`` уже в новом.

        Старый id сеанса заменяется только ответом ``initialize``: параллельные запросы до
        этого идут со старым (получат 404 и дождутся нового). Сбой — ``_params`` пуст, и
        следующий вызов откроет сеанс заново.
        """
        self._params = None
        exchange = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alla-launch", "version": "1"},
        }, session=False)
        result = self._result(exchange, "initialize", had_session=False)
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version not in SUPPORTED_VERSIONS:
            raise McpError(
                "protocol",
                f"сервер предложил версию протокола MCP {version!r}, скилл знает "
                + ", ".join(SUPPORTED_VERSIONS),
            )
        session = exchange.headers.get("mcp-session-id")
        self._session_id = session or None
        self._protocol = version
        self._generation += 1
        notified = await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        self._check_status(notified, had_session=False)
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            listed = self._result(
                await self._request("tools/list", {"cursor": cursor} if cursor else {}),
                "tools/list", had_session=False,
            )
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
        params = _tool_params(tool)
        schema = tool.get("inputSchema")
        required = schema.get("required") if isinstance(schema, dict) else None
        self.project_param = params["project"] is not None
        self.project_required = bool(params["project"]) and isinstance(required, list) \
            and params["project"] in required
        if self.project_required and self.project_id is None:
            raise McpError(
                "no_project",
                f"инструмент {FIND_RESULTS_TOOL} требует проект TestOps ({params['project']}), "
                "а он не задан: нужен ALLURE_PROJECT_ID",
            )
        self._params = params
        return tools

    async def _ensure_ready(self) -> dict[str, str | None]:
        params = self._params
        if params is None:
            async with self._init_lock:
                if self._params is None:
                    await self._initialize()
                params = self._params
        assert params is not None
        return params

    # --- Протокол TestResultsProvider ---

    async def get_launch(self, launch_id: int) -> LaunchResponse:
        """Метаданные прогона из первой страницы его результатов: инструмента прогона в MCP нет.

        Имя и проект — если сервер прислал их в результатах; ``closed`` и время прогона
        неизвестны (``None``: триаж возьмёт время самого раннего результата). Несуществующий
        и пустой прогон по MCP не различить: оба — прогон без результатов (``prepare`` так и
        скажет).
        """
        page = await self.get_test_results_for_launch(launch_id)
        self._first_pages[launch_id] = (page, self._totals_known, self._total_known)
        first = page.content[0] if page.content else None
        extra = (first.model_extra or {}) if first is not None else {}
        launch_raw = extra.get("launch")
        launch: dict[str, Any] = launch_raw if isinstance(launch_raw, dict) else {}
        name = extra.get("launchName") or launch.get("name")
        project = extra.get("projectId") or launch.get("projectId") or self.project_id
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
        arguments = self._arguments(params, f"launch = {launch_id}")
        if params["page"]:
            arguments[params["page"]] = page
        if params["size"]:
            arguments[params["size"]] = size
        payload = await self._call_tool(arguments)
        # Сервер урезал страницу — дальше считаем страницы его размером, а не запрошенным.
        result, self._totals_known, self._total_known = _page(
            payload, page, self._server_page_size or size, paged=bool(params["page"]))
        if result.size < size:
            self._server_page_size = result.size
        return result

    async def get_all_test_results_for_launch(
        self, launch_id: int,
    ) -> list[TestResultResponse]:
        """Все результаты прогона, включая hidden, по страницам; потолок — ``max_pages``.

        Без итогов в ответе — страницы, пока приходят новые id: пустая страница или та же
        (сервер не листает) — конец; ``last_total_elements`` тогда ``None``.
        """
        all_results: list[TestResultResponse] = []
        seen: set[int] = set()
        page = 0
        while True:
            cached = self._first_pages.pop(launch_id, None) if page == 0 else None
            if cached is not None:
                page_resp, known, total_known = cached
            else:
                page_resp = await self.get_test_results_for_launch(
                    launch_id, page=page, size=self._page_size,
                )
                known, total_known = self._totals_known, self._total_known
            ids = {result.id for result in page_resp.content}
            if not known and not ids - seen:
                break
            seen |= ids
            all_results.extend(page_resp.content)
            # Только сообщённое сервером: вычисленное по страницам дало бы ложную «потерю».
            self.last_total_elements = page_resp.total_elements if total_known else None
            if known and page == 0 and page_resp.total_pages > self._max_pages:
                raise PaginationLimitError(
                    f"В прогоне {page_resp.total_elements} результатов "
                    f"({page_resp.total_pages} страниц по {page_resp.size}), "
                    f"лимит — {self._max_pages} страниц.",
                    pages_needed=page_resp.total_pages,
                )
            if page + 1 >= page_resp.total_pages:
                break
            page += 1
            if page >= self._max_pages:
                raise PaginationLimitError(
                    f"Превышен max_pages={self._max_pages}. "
                    f"Получено {len(all_results)}"
                    + (f"/{page_resp.total_elements}" if total_known else "")
                    + " результатов. Увеличьте ALLURE_MAX_PAGES при необходимости.",
                    pages_needed=page_resp.total_pages if known else None,
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
            # Из ответа, а не из памяти: вложения результата больше бюджета вытеснили бы
            # нужное ещё до чтения.
            _, fetched = await self._fetch_full(owner)
            data = fetched.get(attachment_id)
            if data is None:
                raise AllureApiError(404, f"вложение {attachment_id} не пришло в ответе", _ENDPOINT)
        else:
            self._contents.move_to_end(attachment_id)
        if len(data) > cap:
            logger.warning("Вложение %d обрезано: %d байт, лимит %d", attachment_id, len(data), cap)
        return data[:cap]

    # --- Полный результат ---

    async def _full_result(self, test_result_id: int) -> _FullResult:
        lock = self._full_locks.setdefault(test_result_id, asyncio.Lock())
        async with lock:
            cached = self._full.get(test_result_id)
            if cached is not None:
                return cached
            full, _ = await self._fetch_full(test_result_id)
            return full

    async def _fetch_full(self, test_result_id: int) -> tuple[_FullResult, dict[int, bytes]]:
        """Запросить результат с полным expand; кроме разбора — содержимое вложений этого ответа."""
        raw = await self._find_one(test_result_id, expand=True)
        contents: dict[int, bytes] = {}
        full = self._parse_full(test_result_id, raw, contents)
        self._full[test_result_id] = full
        return full, contents

    async def _find_one(self, test_result_id: int, *, expand: bool) -> dict[str, Any]:
        params = await self._ensure_ready()
        arguments = self._arguments(params, f"id = {test_result_id}")
        if expand:
            arguments[params["expand"] or "expand"] = list(FULL_EXPAND)
        page = _items(await self._call_tool(arguments))
        item = next((entry for entry in page if entry.get("id") == test_result_id), None)
        if item is None and len(page) == 1:
            item = page[0]
        if item is None:
            raise AllureApiError(404, f"результат {test_result_id} не найден", _ENDPOINT)
        return item

    def _parse_full(
        self, test_result_id: int, raw: dict[str, Any], contents: dict[int, bytes],
    ) -> _FullResult:
        metas: dict[int, AttachmentMeta] = {}
        attachments = raw.get("attachments")
        entries = (
            [(key, value) for key, value in attachments.items()] if isinstance(attachments, dict)
            else [(None, value) for value in attachments] if isinstance(attachments, list)
            else []
        )
        for key, value in entries:
            meta = self._store_attachment(test_result_id, key, value, contents)
            if meta is not None and meta.id is not None:
                metas[meta.id] = meta
        steps = [_step(item, metas) for item in _scenario_steps(raw.get("scenario"))]
        before, after = _fixtures(raw.get("fixtures"))
        steps = ([_step(item, metas) for item in before] + steps
                 + [_step(item, metas) for item in after])
        body = {key: value for key, value in raw.items()
                if key not in ("scenario", "fixtures", "attachments")}
        return _FullResult(_result(body), steps, list(metas.values()))

    def _store_attachment(
        self, test_result_id: int, key: object, value: object, contents: dict[int, bytes],
    ) -> AttachmentMeta | None:
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
            contents[attachment_id] = data
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

    def _arguments(self, params: dict[str, str | None], aql: str) -> dict[str, Any]:
        """AQL и проект (если задан и есть в схеме инструмента)."""
        arguments: dict[str, Any] = {params["aql"] or "aql": aql}
        if self.project_id is not None and params["project"]:
            arguments[params["project"]] = self.project_id
        return arguments

    # --- JSON-RPC ---

    async def _call_tool(self, arguments: dict[str, Any]) -> Any:
        result = await self._rpc("tools/call", {"name": FIND_RESULTS_TOOL, "arguments": arguments})
        return tool_payload(result, f"{FIND_RESULTS_TOOL} ({arguments})")

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Запрос JSON-RPC в открытом сеансе; 404 (сервер забыл сеанс) — одно новое открытие на
        всех, кто его получил (поколение сеанса), и повтор."""
        await self._ensure_ready()
        generation = self._generation
        exchange = await self._request(method, params)
        if exchange.status == 404 and self._session_id:
            async with self._init_lock:
                if self._generation == generation:
                    logger.info("MCP TestOps: сеанс истёк, открываю новый")
                    await self._initialize()
            exchange = await self._request(method, params)
        return self._result(exchange, method, had_session=True)

    async def _request(
        self, method: str, params: dict[str, Any], *, session: bool = True,
    ) -> _Exchange:
        self._rpc_id += 1
        message = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        return await self._post(message, self._rpc_id, session=session)

    def _result(self, exchange: _Exchange, method: str, *, had_session: bool) -> Any:
        self._check_status(exchange, had_session=had_session)
        reply = exchange.reply
        if reply is None:
            raise McpError("protocol", f"{method}: в ответе нет сообщения JSON-RPC")
        error = reply.get("error")
        if isinstance(error, dict):
            raise McpError("tool", f"{method}: {error.get('message') or error}")
        return reply.get("result")

    def _check_status(self, exchange: _Exchange, *, had_session: bool) -> None:
        status = exchange.status
        if 300 <= status < 400:
            raise AllureApiError(status, "перенаправление TestOps запрещено", "mcp")
        if status < 400:
            return
        if status == 404 and had_session:
            raise McpError("protocol",
                           "сервер не принял сеанс MCP и после нового открытия (HTTP 404)")
        if status == 404:
            raise McpError("not_found", f"по адресу {self._url} нет MCP-сервера (HTTP 404)")
        if status == 405:
            raise McpError(
                "transport",
                f"{self._url} не принимает Streamable HTTP (HTTP 405): скилл не умеет старый "
                "SSE-транспорт MCP",
            )
        raise AllureApiError(status, exchange.text[:500], "mcp")

    async def _post(
        self, message: dict[str, Any], request_id: int | None, *, session: bool = True,
    ) -> _Exchange:
        """POST с повторами: все вызовы читают, поэтому повторяются как GET прежнего клиента.

        Заголовки сеанса — на каждую попытку: за паузу сеанс мог смениться.
        """
        method = str(message.get("method"))
        attempt = 0
        while True:
            headers = {
                "Authorization": f"api-token {self._token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if session and self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            if session and self._protocol:
                headers["MCP-Protocol-Version"] = self._protocol
            try:
                exchange = await self._exchange(message, request_id, headers)
            except httpx.RequestError as exc:
                if attempt >= self._retries:
                    raise AllureApiError(0, str(exc), "mcp") from exc
                await self._pause(method, attempt, "сбой сети", None)
            else:
                if exchange.status not in RETRY_STATUSES or attempt >= self._retries:
                    return exchange
                await self._pause(method, attempt, f"HTTP {exchange.status}",
                                  exchange.headers.get("Retry-After"))
            attempt += 1

    async def _exchange(
        self, message: dict[str, Any], request_id: int | None, headers: dict[str, str],
    ) -> _Exchange:
        """Один POST. Ответ JSON читается целиком; поток SSE — до своего ответа, после чего
        закрывается (сервер не обязан закрывать его сам); запросы сервера в потоке получают
        ответ отдельным POST."""
        require_allowed_origin(self._url, self._allowed_origins)
        async with self._http.stream("POST", self._url, json=message, headers=headers) as resp:
            content_type = (resp.headers.get("content-type") or "").lower()
            if resp.status_code >= 300 or not content_type.startswith("text/event-stream"):
                text = (await resp.aread()).decode("utf-8", errors="replace")
                reply = None
                if resp.status_code < 300 and request_id is not None and text.strip():
                    reply = _json_reply(text, request_id)
                return _Exchange(resp.status_code, resp.headers, text, reply)
            reply = await self._read_sse(resp, request_id, headers)
            return _Exchange(resp.status_code, resp.headers, "", reply)

    async def _read_sse(
        self, resp: httpx.Response, request_id: int | None, headers: dict[str, str],
    ) -> dict[str, Any] | None:
        data: list[str] = []

        async def event_done() -> dict[str, Any] | None:
            if not data:
                return None
            text = "\n".join(data)
            data.clear()
            try:
                message = json.loads(text)
            except ValueError:
                return None
            if not isinstance(message, dict):
                return None
            if request_id is not None and message.get("id") == request_id \
                    and ("result" in message or "error" in message):
                return message
            if "method" in message and "id" in message:
                await self._answer_server(message, headers)
            return None

        # aiter_lines режет и по \r\n, и по одиночным \r и \n — как требует SSE.
        async for line in resp.aiter_lines():
            if line == "":
                found = await event_done()
                if found is not None:
                    return found
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
        found = await event_done()
        if found is not None or request_id is None:
            return found
        raise McpError("protocol", f"в потоке SSE нет ответа на запрос {request_id}")

    async def _answer_server(self, message: dict[str, Any], headers: dict[str, str]) -> None:
        """Ответ на запрос сервера в потоке: ``ping`` — пустой результат, прочее — нет метода."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if message.get("method") == "ping":
            body["result"] = {}
        else:
            body["error"] = {"code": -32601, "message": "Method not found"}
        require_allowed_origin(self._url, self._allowed_origins)
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
            if 300 <= response.status_code < 400:
                raise AllureApiError(response.status_code, "перенаправление TestOps запрещено", "mcp")
        except httpx.HTTPError as exc:
            logger.warning("MCP TestOps: ответ на %s не отправлен: %s", message.get("method"), exc)

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
        return {"aql": "aql", "page": "page", "size": "size", "expand": "expand",
                "project": "projectId"}
    return {
        role: next((name for name in names if name in properties), None)
        for role, names in _PARAM_CANDIDATES.items()
    }


def _json_reply(text: str, request_id: int) -> dict[str, Any]:
    """Ответ на запрос ``request_id`` из тела JSON (одно сообщение или пакет)."""
    try:
        body = json.loads(text)
    except ValueError:
        raise McpError("protocol", f"ответ не JSON: {text[:200]}") from None
    for message in body if isinstance(body, list) else [body]:
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


def _page(
    payload: Any, page: int, size: int, *, paged: bool,
) -> tuple[PageResponse[TestResultResponse], bool, bool]:
    """Страница результатов в ``PageResponse``; известны ли итоги (``totalPages`` или число
    результатов) и отдельно — число результатов (без него ``totalElements`` вычислен).

    Размер страницы — из ответа (сервер может урезать запрошенный), иначе запрошенный; без
    ``size`` в ответе неполная страница при известном итоге, где результатов ещё больше, —
    тоже урезанная сервером. Итогов нет — ещё страница, пока эта не пуста (конец решает
    ``get_all_test_results_for_launch`` по новым id). Без параметра страницы у инструмента
    страница одна: следующие запросы вернули бы её же.
    """
    items = [_result(item) for item in _items(payload)]
    meta = payload if isinstance(payload, dict) else {}
    total = _int(meta.get("totalElements", meta.get("total")))
    actual = _int(meta.get("size"))
    if actual is None or actual <= 0:
        actual = size
        if total is not None and 0 < len(items) < size and page * size + len(items) < total:
            actual = len(items)
    pages = _int(meta.get("totalPages"))
    known = pages is not None or total is not None
    if pages is None and total is not None:
        pages = max(1, math.ceil(total / actual))
    if pages is None:
        pages = page + 2 if items else page + 1
    if not paged:
        pages = page + 1
    return PageResponse[TestResultResponse](
        content=items,
        totalElements=total if total is not None else page * actual + len(items),
        totalPages=pages,
        size=actual,
        number=page,
    ), known, total is not None


def _result(raw: dict[str, Any]) -> TestResultResponse:
    """Модель результата; ``message``/``trace`` верхнего уровня — в ``statusDetails``, если его нет."""
    data = dict(raw)
    if not isinstance(data.get("statusDetails"), dict):
        details = {key: data[key] for key in ("message", "trace") if isinstance(data.get(key), str)}
        if details:
            data["statusDetails"] = details
    return TestResultResponse.model_validate(data)


_STEP_FIELDS = ("name", "status", "statusDetails", "message", "trace", "attachments")


def _scenario_steps(scenario: object) -> list[dict[str, Any]]:
    """Корни дерева шагов: список, ``{steps: [...]}``, ``{content}``.

    Обёртка с любым полем шага (имя, статус, ошибка, вложения) — сама корень: иначе failed-
    корень с passed-подшагами терял путь упавшего шага, а его скриншоты — пометку «упавший
    шаг» (ревью Codex). Только голая обёртка ``{steps: [...]}`` — контейнер.
    """
    if isinstance(scenario, list):
        return [item for item in scenario if isinstance(item, dict)]
    if not isinstance(scenario, dict):
        return []
    if any(scenario.get(key) for key in _STEP_FIELDS):
        return [scenario]
    for key in ("steps", "content", "children"):
        if isinstance(scenario.get(key), list):
            return [item for item in scenario[key] if isinstance(item, dict)]
    return [scenario] if scenario.get("name") else []


def _fixtures(fixtures: object) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Фикстуры: ``{befores, afters}`` или список с ``type``; ``after``/``teardown`` — после теста."""
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    if isinstance(fixtures, dict):
        for key, value in fixtures.items():
            if isinstance(value, list):
                kind = str(key).lower()
                target = after if "after" in kind or "teardown" in kind else before
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
