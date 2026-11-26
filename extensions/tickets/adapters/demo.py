"""Локальный демонстрационный трекер: JSON stdin/stdout, SQLite, без сетевых вызовов.

Подключается явно через adapter_command. prepare и search не создают даже файл БД.
Флаг --lose-response-once теряет первый ответ каждой новой мутации уже после commit.
Это проверка восстановления через lookup, а не настройка настоящего адаптера.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


class ProtocolError(Exception):
    """Ошибка протокола с безопасным текстом для пользователя."""


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


class Demo:
    def __init__(self, path: Path, project: str) -> None:
        self.path = path.resolve()
        self.project = project
        digest = hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:16]
        self.site = f"demo://local/{digest}"

    def scope(self, ticket: dict[str, Any]) -> dict[str, str]:
        team = ticket.get("team")
        if not isinstance(team, str) or not team.strip():
            raise ProtocolError("Для демонстрации укажи команду в черновике")
        return {"site": self.site, "project_or_queue": f"{self.project}/{team}"}

    def checked_scope(self, value: Any) -> dict[str, str]:
        if (not isinstance(value, dict) or set(value) != {"site", "project_or_queue"}
                or value.get("site") != self.site
                or not isinstance(value.get("project_or_queue"), str)
                or not value["project_or_queue"].startswith(self.project + "/")
                or not value["project_or_queue"][len(self.project) + 1:]):
            raise ProtocolError("Область запроса не соответствует демонстрационному трекеру")
        return {"site": str(value["site"]),
                "project_or_queue": str(value["project_or_queue"])}

    def prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        ticket = request.get("ticket")
        if not isinstance(ticket, dict):
            raise ProtocolError("prepare принимает объект ticket")
        if ticket.get("kind") not in {"bug", "task", "investigation"}:
            raise ProtocolError("kind должен быть bug, task или investigation")
        if any(not isinstance(ticket.get(key), str) or not ticket[key].strip()
               for key in ("title", "description")):
            raise ProtocolError("Для демонстрации нужны title и description")
        if ticket["kind"] == "investigation" and any(
                not isinstance(ticket.get(key), str) or not ticket[key].strip()
                for key in ("question", "check")):
            raise ProtocolError("Для исследования нужны question и check")
        action = request.get("action", "create")
        target = request.get("target_id")
        if action not in ("create", "link"):
            raise ProtocolError("prepare поддерживает только create и link")
        if action == "link" and (not isinstance(target, str) or not target):
            raise ProtocolError("Для link нужен target_id из поиска")
        body = {"operation": action, "request_id": request["request_id"],
                "target_id": target, "ticket": ticket}
        payload = base64.b64encode(canonical(body).encode("utf-8")).decode("ascii")
        return {"api": 1, "scope": self.scope(ticket),
                "capabilities": {"search": True, "authoritative_absence": True},
                "payload": payload, "preview": ticket}

    def read_connection(self, *, serialize: bool = False) -> sqlite3.Connection | None:
        if not self.path.is_file():
            return None
        mode = "rw" if serialize else "ro"
        connection = sqlite3.connect("file:" + quote(str(self.path)) + f"?mode={mode}",
                                     uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        if serialize:
            # Пустой SELECT во время незавершённой мутации не доказывает отсутствие.
            # Тот же write-lock дожидается её commit/rollback; сам lookup ничего не меняет.
            connection.execute("BEGIN IMMEDIATE")
        return connection

    def write_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, scope TEXT NOT NULL,
                payload TEXT NOT NULL, ticket_number INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ticket_updates (
                request_id TEXT PRIMARY KEY, ticket_number INTEGER NOT NULL,
                payload TEXT NOT NULL);
        """)
        return connection

    def receipt(self, number: int) -> dict[str, str]:
        key = f"DEMO-{number}"
        return {"id": key, "url": f"{self.site}/tickets/{key}", "state": "open"}

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        scope = canonical(self.checked_scope(request.get("scope")))
        connection = self.read_connection()
        if connection is None:
            return {"api": 1, "matches": []}
        try:
            rows = connection.execute(
                "SELECT number FROM tickets WHERE scope = ? ORDER BY number", (scope,)
            ).fetchall()
        finally:
            connection.close()
        matches = [{**self.receipt(int(row["number"])),
                    "reason": "Тикет в той же демонстрационной очереди"} for row in rows]
        return {"api": 1, "matches": matches}

    def mutate(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        scope = self.checked_scope(request.get("scope"))
        payload = request.get("payload")
        if not isinstance(payload, str):
            raise ProtocolError("Мутация принимает зафиксированный payload в base64")
        try:
            raw = base64.b64decode(payload, validate=True)
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError("payload должен содержать зафиксированный JSON в base64") from exc
        if (not isinstance(body, dict) or body.get("request_id") != request["request_id"]
                or body.get("operation") != request["operation"]
                or not isinstance(body.get("ticket"), dict)
                or self.scope(body["ticket"]) != scope):
            raise ProtocolError("payload не соответствует операции, request_id или области")
        scope_text = canonical(scope)
        connection = self.write_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request["request_id"],)
            ).fetchone()
            if previous is not None:
                if (previous["scope"] != scope_text or previous["payload"] != payload
                        or previous["operation"] != request["operation"]):
                    raise ProtocolError("request_id уже использован с другими байтами")
                number = int(previous["ticket_number"])
                created = False
            else:
                if request["operation"] == "create":
                    cursor = connection.execute(
                        "INSERT INTO tickets(scope,title,payload) VALUES (?,?,?)",
                        (scope_text, body["ticket"]["title"], payload))
                    assert cursor.lastrowid is not None
                    number = cursor.lastrowid
                else:
                    target = body.get("target_id")
                    row = connection.execute(
                        "SELECT number FROM tickets WHERE scope = ? AND 'DEMO-' || number = ?",
                        (scope_text, target)).fetchone()
                    if row is None:
                        raise ProtocolError("Тикет для link не найден в этой очереди")
                    number = int(row["number"])
                    connection.execute(
                        "INSERT INTO ticket_updates(request_id,ticket_number,payload) VALUES (?,?,?)",
                        (request["request_id"], number, payload))
                connection.execute(
                    "INSERT INTO requests VALUES (?,?,?,?,?)",
                    (request["request_id"], request["operation"], scope_text, payload, number))
                created = True
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"api": 1, "outcome": "found", "ticket": self.receipt(number)}, created

    def lookup(self, request: dict[str, Any]) -> dict[str, Any]:
        scope = self.checked_scope(request.get("scope"))
        action = request.get("action")
        if action not in ("create", "link"):
            raise ProtocolError("lookup принимает исходный action")
        connection = self.read_connection(serialize=True)
        if connection is None:
            return {"api": 1, "outcome": "absent"}
        try:
            row = connection.execute(
                "SELECT operation,scope,ticket_number FROM requests WHERE request_id = ?",
                (request["request_id"],)).fetchone()
        finally:
            connection.close()
        if row is None:
            return {"api": 1, "outcome": "absent"}
        if row["scope"] != canonical(scope) or row["operation"] != action:
            return {"api": 1, "outcome": "unknown"}
        return {"api": 1, "outcome": "found", "ticket": self.receipt(int(row["ticket_number"]))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite-файл демонстрационного трекера")
    parser.add_argument("--project", default="DEMO", help="стабильное имя проекта демонстрации")
    parser.add_argument("--lose-response-once", action="store_true",
                        help="после первой мутации request_id завершиться без ответа")
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(request, dict) or type(request.get("api")) is not int or request["api"] != 1:
            raise ProtocolError("Поддерживается JSON-объект с api = 1")
        request["request_id"] = request_uuid(request.get("request_id"))
        demo = Demo(args.db, args.project)
        operation = request.get("operation")
        if operation == "prepare":
            response = demo.prepare(request)
        elif operation == "search":
            response = demo.search(request)
        elif operation == "lookup":
            response = demo.lookup(request)
        elif operation in ("create", "link"):
            response, changed = demo.mutate(request)
            if changed and args.lose_response_once:
                # Commit уже завершён. Lookup обязан найти его после потери ответа.
                os._exit(75)
        else:
            raise ProtocolError("Неизвестная операция адаптера")
        print(canonical(response))
        return 0
    except (ProtocolError, ValueError, UnicodeDecodeError):
        print(canonical({"api": 1, "error": "Запрос не соответствует контракту адаптера"}))
        return 1
    except (OSError, sqlite3.Error):
        print(canonical({"api": 1, "error": "Демонстрационный трекер недоступен"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
