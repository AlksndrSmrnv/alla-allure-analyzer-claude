"""Универсальный адаптер tickets для MCP-сервера трекера (stdio, api = 1).

Подключается явно через adapter_command: `mcp.py --config alla-extensions/mcp-tracker.toml`.
Сам запускает MCP-сервер из конфигурации, говорит с ним JSON-RPC по stdin/stdout и
сопоставляет нейтральный тикет с вызовами его инструментов по шаблонам файла.
`prepare` чистый и сервер не запускает. Метка `alla-request: UUID` попадает в текст
тикета и служит ключом `lookup`; достоверного отсутствия у MCP нет, поэтому `absent`
адаптер не возвращает никогда. Формат файла — ../README.md, пример — mcp-tracker.example.toml.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

API = 1
MARKER = "alla-request: "
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_BUDGET_S = 20.0
GRACE_S = 3.0
MAX_MATCHES = 20
SAFE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
TICKET_VARS = ("title", "title_q", "body", "team", "project", "tracker_kind", "target_id",
               "request_id", "launch_id", "site")
LOOKUP_VARS = ("project", "request_id", "site")
URL_VARS = ("id", "site")
RESULT_KEYS = {"id_path", "id_regex", "url_path", "url_template", "state_path", "state",
               "items_path", "verify_path", "empty_regex"}


class ProtocolError(Exception):
    """Ошибка конфигурации или запроса с безопасным текстом для пользователя."""


class McpError(Exception):
    """Сбой MCP-сервера с безопасным текстом: ответ сервера наружу не попадает."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_uuid(value: Any) -> str:
    try:
        result = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProtocolError("request_id должен быть UUID вызывающего скрипта") from exc
    if result != value:
        raise ProtocolError("request_id должен быть каноническим UUID")
    return result


def scalar(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def get_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def render(value: Any, variables: dict[str, str]) -> Any:
    """Подставляет {имя} за один проход; неизвестное имя — ошибка, а не пустая строка."""
    if isinstance(value, str):
        def substitute(match: re.Match[str]) -> str:
            if match.group(1) not in variables:
                raise ProtocolError(f"В mcp-tracker.toml неизвестная подстановка {{{match.group(1)}}}")
            return variables[match.group(1)]
        return PLACEHOLDER.sub(substitute, value)
    if isinstance(value, dict):
        return {str(key): render(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, variables) for item in value]
    return value


def quoted(value: str) -> str:
    """Строка для вставки в кавычки языка запросов трекера."""
    return " ".join(value.split()).replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class Result:
    id_path: str = ""
    id_regex: str = ""
    url_path: str = ""
    url_template: str = ""
    state_path: str = ""
    state: str = "open"
    items_path: str = ""
    verify_path: str = ""
    empty_regex: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    arguments: dict[str, Any]
    result: Result


@dataclass(frozen=True)
class Config:
    site: str
    command: list[str]
    extra_path: list[str]
    budget: float
    projects: dict[str, str]
    kinds: dict[str, str]
    create: Tool
    link: Tool | None
    search: Tool | None
    lookup: Tool | None
    lookup_link: Tool | None


def text_field(section: dict[str, Any], key: str, where: str, *, required: bool = False,
               default: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or (required and not value.strip()):
        raise ProtocolError(f"В mcp-tracker.toml нужна строка {key} в разделе {where}")
    return value


def string_map(data: dict[str, Any], key: str, *, required: bool = False) -> dict[str, str]:
    value = data.get(key, {})
    if (not isinstance(value, dict) or (required and not value)
            or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                       for k, v in value.items())):
        raise ProtocolError(f"В mcp-tracker.toml раздел [{key}] — таблица «ключ = строка»")
    return value


def load_result(section: dict[str, Any], where: str) -> Result:
    raw = section.get("result", {})
    if not isinstance(raw, dict) or set(raw) - RESULT_KEYS:
        raise ProtocolError(f"В [{where}.result] неизвестный ключ или не таблица")
    result = Result(**{key: text_field(raw, key, f"{where}.result") for key in raw})
    for pattern in (result.id_regex, result.empty_regex):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProtocolError(f"В [{where}.result] неверное регулярное выражение") from exc
    render(result.url_template, dict.fromkeys(URL_VARS, ""))
    return result


def load_tool(data: dict[str, Any], name: str, variables: tuple[str, ...]) -> Tool | None:
    section = data.get(name)
    if section is None:
        return None
    if not isinstance(section, dict) or set(section) - {"tool", "arguments", "result"}:
        raise ProtocolError(f"В mcp-tracker.toml раздел [{name}] содержит неизвестный ключ")
    arguments = section.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ProtocolError(f"В [{name}] arguments — таблица")
    render(arguments, dict.fromkeys(variables, ""))
    return Tool(text_field(section, "tool", name, required=True), arguments,
                load_result(section, name))


def check_result(name: str, tool: Tool | None, *, ticket_id: str = "path") -> None:
    """ticket_id: «path» — id_path, «any» — id_path или id_regex, «none» — id берётся из target."""
    if tool is None:
        return
    result = tool.result
    if ticket_id == "path" and not result.id_path:
        raise ProtocolError(f"В [{name}.result] нужен id_path")
    if ticket_id == "any" and not (result.id_path or result.id_regex):
        raise ProtocolError(f"В [{name}.result] нужен id_path или id_regex")
    if not (result.url_path or result.url_template):
        raise ProtocolError(f"В [{name}.result] нужен url_path или url_template")


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProtocolError("Не удалось прочитать файл mcp-tracker.toml") from exc
    allowed = {"site", "server", "projects", "kinds", "create", "link", "search", "lookup",
               "lookup_link"}
    if set(data) - allowed:
        raise ProtocolError("В mcp-tracker.toml неизвестный раздел")
    site = text_field(data, "site", "корне", required=True)
    server = data.get("server")
    if not isinstance(server, dict) or set(server) - {"command", "extra_path", "timeout"}:
        raise ProtocolError("В mcp-tracker.toml нужен раздел [server] с командой сервера")
    command = server.get("command")
    extra = server.get("extra_path", [])
    budget = server.get("timeout", DEFAULT_BUDGET_S)
    if (not isinstance(command, list) or not command
            or not all(isinstance(item, str) and item for item in command)):
        raise ProtocolError("В [server] command — непустой список строк")
    if not isinstance(extra, list) or not all(isinstance(item, str) and item for item in extra):
        raise ProtocolError("В [server] extra_path — список каталогов")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1 <= budget <= 600:
        raise ProtocolError("В [server] timeout — число секунд от 1 до 600")
    create = load_tool(data, "create", TICKET_VARS)
    if create is None:
        raise ProtocolError("В mcp-tracker.toml нет раздела [create]")
    config = Config(
        site=site, command=command, extra_path=extra, budget=float(budget),
        projects=string_map(data, "projects", required=True), kinds=string_map(data, "kinds"),
        create=create, link=load_tool(data, "link", TICKET_VARS),
        search=load_tool(data, "search", TICKET_VARS),
        lookup=load_tool(data, "lookup", LOOKUP_VARS),
        lookup_link=load_tool(data, "lookup_link", LOOKUP_VARS))
    check_result("create", config.create, ticket_id="any")
    check_result("link", config.link, ticket_id="none")
    for name in ("search", "lookup", "lookup_link"):
        check_result(name, getattr(config, name))
    return config


@dataclass
class Reply:
    text: str
    data: Any


def reply_of(result: dict[str, Any]) -> Reply:
    if result.get("isError") is True:
        raise McpError("Инструмент MCP вернул ошибку")
    content = result.get("content")
    parts = [item["text"] for item in (content if isinstance(content, list) else [])
             if isinstance(item, dict) and item.get("type") == "text"
             and isinstance(item.get("text"), str)]
    text = "\n".join(parts)
    data = result.get("structuredContent")
    if not isinstance(data, (dict, list)):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return Reply(text, data)


class Session:
    """Один запуск MCP-сервера на вызов адаптера; закрывается при выходе."""

    def __init__(self, config: Config, root: Path, tools: list[str]) -> None:
        self.config = config
        self.root = root
        self.tools = tools
        self.deadline = time.monotonic() + config.budget
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.finished = False
        self.next_id = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None

    def __enter__(self) -> Session:
        command, env = self.launch_spec()
        try:
            # close_fds=False: сервер наследует дескриптор блокировки журнала, как велит README.
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, cwd=self.root, env=env,
                                            close_fds=False)
        except OSError as exc:
            raise McpError("Не удалось запустить MCP-сервер") from exc
        self.reader = threading.Thread(target=self.pump, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": {"name": "alla-tickets", "version": "1"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.require_tools()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        self.close()

    def launch_spec(self) -> tuple[list[str], dict[str, str]]:
        directories = [str(Path(item) if os.path.isabs(item) else self.root / item)
                       for item in self.config.extra_path] + list(SAFE_PATH)
        first = self.config.command[0]
        if "/" in first:
            program: Path | None = Path(first) if os.path.isabs(first) else self.root / first
        else:
            found = shutil.which(first, path=os.pathsep.join(directories))
            program = Path(found) if found else None
        if program is None or not program.is_file() or not os.access(program, os.X_OK):
            raise ProtocolError("Команда MCP-сервера не найдена: задай путь в [server] command "
                                "или каталог в extra_path")
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(program.parent), *directories])
        return [str(program), *self.config.command[1:]], env

    def pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            self.process.stdin.write((canonical(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as exc:
            raise McpError("MCP-сервер закрыл вход") from exc

    def receive(self) -> dict[str, Any]:
        while True:
            remaining = self.deadline - time.monotonic()
            if self.finished or remaining <= 0:
                raise McpError("MCP-сервер не ответил вовремя" if not self.finished
                               else "MCP-сервер завершился без ответа")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise McpError("MCP-сервер не ответил вовремя") from exc
            if line is None:
                self.finished = True
                raise McpError("MCP-сервер завершился без ответа")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue  # посторонняя строка (лог npx и т.п.): не часть протокола
            if isinstance(message, dict):
                return message

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        ident = self.next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            message = self.receive()
            if "method" in message and "id" in message:
                self.send({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32601, "message": "not supported"}})
            elif message.get("id") == ident and "method" not in message:
                result = message.get("result")
                if "error" in message or not isinstance(result, dict):
                    raise McpError(f"MCP-сервер отклонил запрос {method}")
                return result

    def require_tools(self) -> None:
        names: set[str] = set()
        cursor: str | None = None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                raise McpError("MCP-сервер вернул неверный список инструментов")
            names.update(item["name"] for item in tools
                         if isinstance(item, dict) and isinstance(item.get("name"), str))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        missing = sorted(set(self.tools) - names)
        if missing:
            raise ProtocolError("У MCP-сервера нет инструмента из mcp-tracker.toml: "
                                + ", ".join(missing))

    def call(self, name: str, arguments: dict[str, Any]) -> Reply:
        return reply_of(self.request("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        for stop in (None, process.terminate, process.kill):
            if stop is not None:
                stop()
            try:
                process.wait(timeout=GRACE_S)
                break
            except subprocess.TimeoutExpired:
                continue
        if self.reader is not None:
            self.reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


def ticket_of(request: dict[str, Any]) -> dict[str, Any]:
    ticket = request.get("ticket")
    if not isinstance(ticket, dict):
        raise ProtocolError("Операция принимает объект ticket")
    if ticket.get("kind") not in {"bug", "task", "investigation"}:
        raise ProtocolError("kind должен быть bug, task или investigation")
    if any(not isinstance(ticket.get(key), str) or not ticket[key].strip()
           for key in ("title", "description", "team")):
        raise ProtocolError("Нужны title, description и team")
    if ticket["kind"] == "investigation" and any(
            not isinstance(ticket.get(key), str) or not ticket[key].strip()
            for key in ("question", "check")):
        raise ProtocolError("Для исследования нужны question и check")
    return ticket


def action_of(request: dict[str, Any]) -> tuple[str, str | None]:
    action = request.get("action", "create")
    target = request.get("target_id")
    if action not in ("create", "link"):
        raise ProtocolError("Поддерживаются только create и link")
    if action == "link" and (not isinstance(target, str) or not target):
        raise ProtocolError("Для link нужен target_id из поиска")
    return action, target if isinstance(target, str) else None


def project_of(config: Config, ticket: dict[str, Any]) -> str:
    project = config.projects.get(ticket["team"])
    if project is None:
        raise ProtocolError(f"Команда {ticket['team']} не сопоставлена с проектом в [projects]")
    return project


def ticket_variables(config: Config, ticket: dict[str, Any], request_id: str,
                     target: str | None) -> dict[str, str]:
    project = project_of(config, ticket)
    return {"title": ticket["title"], "title_q": quoted(ticket["title"]),
            "body": f"{ticket['description']}\n\n{MARKER}{request_id}",
            "team": ticket["team"], "project": project,
            "tracker_kind": config.kinds.get(ticket["kind"], ticket["kind"]),
            "target_id": target or "", "request_id": request_id,
            "launch_id": str(ticket.get("launch_id", "")), "site": config.site}


def scope_of(config: Config, project: str) -> dict[str, str]:
    return {"site": config.site, "project_or_queue": project}


def checked_scope(config: Config, value: Any) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != {"site", "project_or_queue"}
            or value.get("site") != config.site
            or value.get("project_or_queue") not in set(config.projects.values())):
        raise ProtocolError("Область запроса не соответствует mcp-tracker.toml")
    return {"site": str(value["site"]), "project_or_queue": str(value["project_or_queue"])}


def receipt(config: Config, result: Result, ticket_id: str, source: Any) -> dict[str, str]:
    url = scalar(get_path(source, result.url_path)) if result.url_path else None
    if url is None and result.url_template:
        url = str(render(result.url_template, {"id": ticket_id, "site": config.site}))
    state = scalar(get_path(source, result.state_path)) if result.state_path else None
    if not url:
        raise McpError("В ответе MCP нет ссылки на тикет")
    return {"id": ticket_id, "url": url, "state": state or result.state}


def matches_of(config: Config, tool: Tool, reply: Reply,
               marker: str | None = None) -> list[dict[str, str]]:
    result = tool.result
    if result.empty_regex and re.search(result.empty_regex, reply.text):
        return []
    items = get_path(reply.data, result.items_path) if result.items_path else reply.data
    if not isinstance(items, list):
        raise McpError("Поиск MCP не вернул список")  # сбой не выдаётся за пустой поиск
    found: dict[str, dict[str, str]] = {}
    for item in items:
        ticket_id = scalar(get_path(item, result.id_path))
        if ticket_id is None:
            raise McpError("В ответе поиска MCP нет идентификатора тикета")
        if marker is not None and result.verify_path:
            verified = get_path(item, result.verify_path)
            if not isinstance(verified, str) or marker not in verified:
                continue
        found[ticket_id] = receipt(config, result, ticket_id, item)
    return [found[key] for key in sorted(found)]


def lookup_matches(config: Config, session: Session, tool: Tool, project: str,
                   request_id: str) -> list[dict[str, str]]:
    arguments = render(tool.arguments, {"project": project, "request_id": request_id,
                                        "site": config.site})
    return matches_of(config, tool, session.call(tool.name, arguments), MARKER + request_id)


def prepare(config: Config, request: dict[str, Any]) -> dict[str, Any]:
    ticket = ticket_of(request)
    action, target = action_of(request)
    tool = config.create if action == "create" else config.link
    if tool is None:
        raise ProtocolError("Для link в mcp-tracker.toml нужен раздел [link]")
    request_id = str(request["request_id"])
    variables = ticket_variables(config, ticket, request_id, target)
    arguments = render(tool.arguments, variables)
    if MARKER + request_id not in canonical(arguments):
        raise ProtocolError("Аргументы инструмента должны содержать {body}: без метки запроса "
                            "нельзя найти тикет после обрыва")
    body = {"operation": action, "request_id": request_id, "target_id": target,
            "site": config.site, "project": variables["project"], "tool": tool.name,
            "arguments": arguments}
    payload = base64.b64encode(canonical(body).encode("utf-8")).decode("ascii")
    return {"api": API, "scope": scope_of(config, variables["project"]),
            "capabilities": {"search": config.search is not None, "authoritative_absence": False},
            "payload": payload}


def search(config: Config, root: Path, request: dict[str, Any]) -> dict[str, Any]:
    ticket = ticket_of(request)
    _, target = action_of(request)
    tool = config.search
    if tool is None:
        raise ProtocolError("Поиск не настроен: в mcp-tracker.toml нет раздела [search]")
    request_id = str(request["request_id"])
    variables = ticket_variables(config, ticket, request_id, target)
    if checked_scope(config, request.get("scope")) != scope_of(config, variables["project"]):
        raise ProtocolError("Область поиска не соответствует команде тикета")
    with Session(config, root, [tool.name]) as session:
        reply = session.call(tool.name, render(tool.arguments, variables))
    reason = "Найдено поиском MCP трекера"
    return {"api": API, "matches": [{**row, "reason": reason}
                                    for row in matches_of(config, tool, reply)[:MAX_MATCHES]]}


def mutate(config: Config, root: Path, request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    scope = checked_scope(config, request.get("scope"))
    try:
        body = json.loads(base64.b64decode(str(request.get("payload")), validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("payload должен содержать зафиксированный JSON в base64") from exc
    tool = config.create if operation == "create" else config.link
    lookup = config.lookup if operation == "create" else config.lookup_link
    if (tool is None or not isinstance(body, dict) or body.get("operation") != operation
            or body.get("request_id") != request["request_id"] or body.get("site") != config.site
            or body.get("project") != scope["project_or_queue"] or body.get("tool") != tool.name
            or not isinstance(body.get("arguments"), dict)):
        raise ProtocolError("payload не соответствует операции, request_id, области или настройке")
    request_id = str(request["request_id"])
    target = body.get("target_id")
    if operation == "link" and not isinstance(target, str):
        raise ProtocolError("payload link не содержит target_id")
    with Session(config, root, [tool.name] + ([lookup.name] if lookup else [])) as session:
        if lookup is not None:
            # Повторная доставка того же запроса возвращает уже созданный тикет.
            earlier = lookup_matches(config, session, lookup, scope["project_or_queue"], request_id)
            if len(earlier) > 1:
                raise McpError("Метка запроса найдена в нескольких тикетах")
            if earlier:
                return {"api": API, "outcome": "found", "ticket": earlier[0]}
        reply = session.call(tool.name, body["arguments"])
    if operation == "link":
        return {"api": API, "outcome": "found",
                "ticket": receipt(config, tool.result, str(target), reply.data)}
    result = tool.result
    ticket_id = (scalar(get_path(reply.data, result.id_path)) if result.id_path
                 else regex_id(result.id_regex, reply.text))
    if ticket_id is None:
        raise McpError("В ответе MCP нет идентификатора созданного тикета")
    return {"api": API, "outcome": "found",
            "ticket": receipt(config, result, ticket_id, reply.data)}


def regex_id(pattern: str, text: str) -> str | None:
    found = re.search(pattern, text)
    if found is None:
        return None
    return scalar(found.group(1) if found.groups() else found.group(0))


def lookup(config: Config, root: Path, request: dict[str, Any]) -> dict[str, Any]:
    scope = checked_scope(config, request.get("scope"))
    action = request.get("action")
    if action not in ("create", "link"):
        raise ProtocolError("lookup принимает исходный action")
    tool = config.lookup if action == "create" else config.lookup_link
    if tool is None:
        return {"api": API, "outcome": "unknown"}
    with Session(config, root, [tool.name]) as session:
        found = lookup_matches(config, session, tool, scope["project_or_queue"],
                               str(request["request_id"]))
    # Пустой ответ — не отсутствие: индекс трекера отстаёт. Несколько совпадений — тоже неясно.
    if len(found) == 1:
        return {"api": API, "outcome": "found", "ticket": found[0]}
    return {"api": API, "outcome": "unknown"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True,
                        help="mcp-tracker.toml: путь от корня проекта")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        config = load_config(root / args.config)
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(request, dict) or type(request.get("api")) is not int or request["api"] != API:
            raise ProtocolError("Поддерживается JSON-объект с api = 1")
        request["request_id"] = request_uuid(request.get("request_id"))
        operation = request.get("operation")
        if operation == "prepare":
            response = prepare(config, request)
        elif operation == "search":
            response = search(config, root, request)
        elif operation == "lookup":
            response = lookup(config, root, request)
        elif operation in ("create", "link"):
            response = mutate(config, root, request)
        else:
            raise ProtocolError("Неизвестная операция адаптера")
        print(canonical(response))
        return 0
    except (ProtocolError, McpError) as exc:
        message = str(exc)
    except (ValueError, UnicodeError):
        message = "Запрос не соответствует контракту адаптера"
    print(canonical({"api": API, "error": message}))
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
