"""Фейковый MCP-сервер трекера по stdio для проверки adapters/mcp.py (JSON-RPC построчно).

Состояние — SQLite. Инструменты: create_issue, add_comment, search_issues. Флаги имитируют
сбои: --lose-response-once (процесс завершается после commit, не ответив), --search-blind
(поиск всегда пуст, как отстающий индекс), --delay (задержка tools/call), --require-env
(без переменной сервер не стартует), --probe-fd (записывает, открыт ли дескриптор).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

TOOLS = [{"name": name, "description": name, "inputSchema": {"type": "object"}}
         for name in ("create_issue", "add_comment", "search_issues")]


def answer(identifier: Any, result: dict[str, Any]) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}), flush=True)


def text_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def issue_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"key": row["key"], "status": "Open", "summary": row["summary"],
            "description": row["description"]}


def call(connection: sqlite3.Connection, args: argparse.Namespace, name: str,
         arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "create_issue":
        if "FAIL" in str(arguments.get("summary")):
            return text_result("tracker refused", error=True)
        cursor = connection.execute(
            "INSERT INTO issues(project,type,summary,description,comments) VALUES (?,?,?,?,'')",
            (arguments["project"], arguments["type"], arguments["summary"],
             arguments["description"]))
        connection.commit()
        if args.lose_response_once:
            os._exit(75)
        return text_result({"key": f"TRK-{cursor.lastrowid}"})
    if name == "add_comment":
        row = connection.execute("SELECT key FROM issues WHERE 'TRK-' || key = ?",
                                 (arguments["issue"],)).fetchone()
        if row is None:
            return text_result("no such issue", error=True)
        connection.execute("UPDATE issues SET comments = comments || ? WHERE 'TRK-' || key = ?",
                           (arguments["text"] + "\n", arguments["issue"]))
        connection.commit()
        if args.lose_response_once:
            os._exit(75)
        return text_result({"ok": True})
    if name == "search_issues":
        query = str(arguments["query"])
        project = re.search(r'project = "([^"]*)"', query)
        needle = re.search(r'text ~ "((?:[^"\\]|\\.)*)"', query)
        if project is None or needle is None:
            return text_result("bad query", error=True)
        text = re.sub(r"\\(.)", r"\1", needle.group(1)).lower()
        if args.search_blind:
            return text_result({"issues": []})
        rows = connection.execute("SELECT * FROM issues WHERE project = ?",
                                  (project.group(1),)).fetchall()
        hits = [row for row in rows if text in (row["summary"] + row["description"]
                                                + row["comments"]).lower()]
        return text_result({"issues": [{**issue_row(row), "key": f"TRK-{row['key']}"}
                                       for row in hits]})
    return text_result("unknown tool", error=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--lose-response-once", action="store_true")
    parser.add_argument("--search-blind", action="store_true")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--require-env", default="")
    parser.add_argument("--probe-fd", nargs=2, metavar=("FD", "PATH"))
    args = parser.parse_args()
    if args.require_env and not os.environ.get(args.require_env):
        return 3
    if args.probe_fd:
        try:
            os.fstat(int(args.probe_fd[0]))
            state = "open"
        except OSError:
            state = "closed"
        Path(args.probe_fd[1]).write_text(state, encoding="utf-8")
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS issues (key INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " project TEXT, type TEXT, summary TEXT, description TEXT, comments TEXT)")
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        if method == "initialize":
            answer(message["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "fake-tracker", "version": "1"}})
        elif method == "tools/list":
            answer(message["id"], {"tools": TOOLS})
        elif method == "tools/call":
            time.sleep(args.delay)
            params = message["params"]
            answer(message["id"], call(connection, args, params["name"], params["arguments"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
