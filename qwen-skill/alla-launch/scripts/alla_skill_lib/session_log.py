"""Журнал сеанса Qwen Code: что агент на самом деле делал во время разбора.

Qwen Code (≥ 0.25) сам пишет журнал каждого сеанса:
``<QWEN_HOME или ~/.qwen>/projects/<путь проекта, не-буквы и не-цифры → «-»>/chats/<сеанс>.jsonl``,
субагенты — ``…/subagents/<сеанс>/agent-*.jsonl`` рядом с ``*.meta.json``. Shell-командам
он передаёт ``QWEN_CODE_SESSION_ID`` и ``QWEN_CODE_PROJECT_DIR``: ``prepare`` и ``next``
запоминают их в ``state.json`` (``note_session``), и проверка разбора (``review``) находит
журнал даже из другого сеанса или из терминала.

Здесь журнал превращается в общую модель вызовов (``session_rules.ToolCall``) и
ограничивается ходами одного разбора. Ход — всё от реплики пользователя до следующей; ход
относится к разбору, если в нём есть команда скилла, назвавшая папку разбора, и берётся
целиком (упавший ``next`` без папки в выводе — тоже). Ходы другой работы между сеансами
разбора в окно не входят.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from alla_skill_lib.session_rules import DONE_RE, SHELL_TOOL, ToolCall, command_of

SESSION_ENV = "QWEN_CODE_SESSION_ID"
PROJECT_DIR_ENV = "QWEN_CODE_PROJECT_DIR"
QWEN_HOME_ENV = "QWEN_HOME"
# Сколько сеансов помнить на один разбор: продолжение после сбоя — новый сеанс.
MAX_SESSIONS = 20
PROBLEMS_HEADING = "Проблемы скилла:"
SUBAGENT_PROBLEM = "Проблема скилла:"
# Команды проверки разбора в окно разбора не входят.
REVIEW_COMMAND_RE = re.compile(r"alla_skill\.py\S*\s+review\b")
_SHELL_OUTPUT_RE = re.compile(r"^Output: ?(.*?)(?:\nError: .*)?(?:\nExit Code: .*)?\Z", re.DOTALL | re.MULTILINE)
_STATUS_RE = re.compile(r"^STATUS: (\w+)\s*$", re.MULTILINE)

logger = logging.getLogger(__name__)


# --- где журнал ---------------------------------------------------------------------------

def note_session(state: dict[str, Any], environ: Mapping[str, str] | None = None) -> bool:
    """Запомнить сеанс Qwen, из которого вызвана команда. True — ``state`` изменился."""
    env = os.environ if environ is None else environ
    session = env.get(SESSION_ENV, "").strip()
    if not session:
        return False
    changed = False
    sessions = [str(item) for item in state.get("sessions", [])]
    if session not in sessions:
        state["sessions"] = (sessions + [session])[-MAX_SESSIONS:]
        changed = True
    project_dir = env.get(PROJECT_DIR_ENV, "").strip()
    if project_dir and state.get("qwen_project_dir") != project_dir:
        state["qwen_project_dir"] = project_dir
        changed = True
    return changed


def sanitize_cwd(path: str) -> str:
    """Имя папки проекта в ``~/.qwen/projects`` — как ``sanitizeCwd`` Qwen Code."""
    normalized = path.lower() if sys.platform == "win32" else path
    return re.sub(r"[^a-zA-Z0-9]", "-", normalized)


def qwen_project_dirs(
    project_root: Path, state: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Где искать журналы проекта: запомненная папка, текущая из окружения, вычисленная."""
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    for raw in (state.get("qwen_project_dir"), env.get(PROJECT_DIR_ENV)):
        if raw:
            candidates.append(Path(str(raw)))
    home = env.get(QWEN_HOME_ENV)
    base = Path(home).expanduser() if home else Path.home() / ".qwen"
    candidates.append(base / "projects" / sanitize_cwd(str(project_root)))
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


# --- журнал -------------------------------------------------------------------------------

@dataclass
class Usage:
    requests: int = 0
    prompt: int = 0
    cached: int = 0
    output: int = 0
    thoughts: int = 0
    total: int = 0

    def add(self, meta: Mapping[str, Any]) -> None:
        self.requests += 1
        self.prompt += _int(meta.get("promptTokenCount"))
        self.cached += _int(meta.get("cachedContentTokenCount"))
        self.output += _int(meta.get("candidatesTokenCount"))
        self.thoughts += _int(meta.get("thoughtsTokenCount"))
        self.total += _int(meta.get("totalTokenCount"))


@dataclass
class _Entry:
    """Событие журнала в порядке времени."""

    timestamp: str
    kind: str  # call | result | text | user | usage | api_error
    subagent: bool = False
    session: str = ""
    call: ToolCall | None = None
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionLog:
    """Сеанс разбора по журналу Qwen, только окно этого разбора."""

    journals: list[Path] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    final: str = ""
    started: str = ""
    finished: str = ""
    # Сумма длительностей ходов разбора, без перерывов и посторонней работы между ними.
    duration_seconds: int | None = None
    turns: int = 0
    reached_done: bool = False
    model: str = ""
    version: str = ""
    usage: Usage = field(default_factory=Usage)
    api_errors: int = 0
    subagents: int = 0
    skill_problems: list[str] = field(default_factory=list)
    note: str = ""


def load_session(
    run_root: Path, project_root: Path, state: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> SessionLog:
    """Журнал разбора ``run_root``. Нет журнала — ``SessionLog`` с пояснением в ``note``."""
    sessions = [str(item) for item in state.get("sessions", [])]
    if not sessions:
        return SessionLog(note="разбор шёл не из Qwen Code или до записи сеанса — журнал неизвестен")
    found: list[tuple[str, Path, Path]] = []  # (сеанс, журнал, папка проекта Qwen)
    for session in sessions:
        for project_dir in qwen_project_dirs(project_root, state, environ):
            chat = project_dir / "chats" / f"{session}.jsonl"
            if chat.is_file():
                found.append((session, chat, project_dir))
                break
    if not found:
        return SessionLog(note=f"журнал сеанса Qwen не найден (сеансов: {len(sessions)})")
    log = SessionLog(journals=[chat for _, chat, _ in found])
    entries: list[_Entry] = []
    for session, chat, project_dir in found:
        entries += _read_journal(chat, subagent=False, log=log, session=session)
        subagent_dir = project_dir / "subagents" / session
        for journal in sorted(subagent_dir.glob("agent-*.jsonl")) if subagent_dir.is_dir() else []:
            entries += _read_journal(journal, subagent=True, log=log, session=session)
            log.journals.append(journal)
    entries.sort(key=lambda entry: entry.timestamp)
    _cut_window(log, entries, str(run_root))
    return log


def _read_journal(path: Path, *, subagent: bool, log: SessionLog, session: str) -> list[_Entry]:
    entries: list[_Entry] = []
    pending: dict[str, ToolCall] = {}
    for record in _records(path):
        timestamp = str(record.get("timestamp", ""))
        kind = record.get("type")
        if not subagent:
            log.version = str(record.get("version") or log.version)
        if kind == "assistant":
            if not subagent and record.get("model"):
                log.model = str(record["model"])
            meta = record.get("usageMetadata")
            if isinstance(meta, dict):
                entries.append(_Entry(timestamp, "usage", subagent, usage=meta))
            for part in _parts(record):
                call_part = part.get("functionCall")
                if isinstance(call_part, dict):
                    args = call_part.get("args")
                    call = ToolCall(0, str(call_part.get("name", "")),
                                    dict(args) if isinstance(args, dict) else {},
                                    subagent=subagent, timestamp=timestamp)
                    pending[str(call_part.get("id", ""))] = call
                    entries.append(_Entry(timestamp, "call", subagent, call=call))
                elif isinstance(part.get("text"), str) and not part.get("thought"):
                    entries.append(_Entry(timestamp, "text", subagent, text=part["text"]))
        elif kind == "tool_result":
            # Время конца команды: ход, который кончается долгим prepare, иначе потерял бы его.
            entries.append(_Entry(timestamp, "result", subagent))
            status = (record.get("toolCallResult") or {}).get("status")
            display = (record.get("toolCallResult") or {}).get("resultDisplay")
            for part in _parts(record):
                response = part.get("functionResponse")
                if not isinstance(response, dict):
                    continue
                found = pending.pop(str(response.get("id", "")), None)
                if found is None:
                    continue
                found.result = _result_text(found, response.get("response"), display)
                found.is_error = status == "error"
        elif kind == "user" and record.get("provenance") == "real_user" and not subagent:
            entries.append(_Entry(timestamp, "user"))
        elif kind == "system" and record.get("subtype") == "ui_telemetry":
            event = (record.get("systemPayload") or {}).get("uiEvent") or {}
            if event.get("event.name") == "qwen-code.api_error":
                entries.append(_Entry(timestamp, "api_error", subagent))
    for entry in entries:
        entry.session = session
    return entries


@dataclass
class _Turn:
    """Ход разговора одного сеанса: от реплики пользователя до следующей."""

    session: str
    start: str
    until: str | None  # время следующей реплики пользователя; None — до конца журнала
    entries: list[_Entry] = field(default_factory=list)

    def holds(self, entry: _Entry) -> bool:
        return (entry.session == self.session and entry.timestamp >= self.start
                and (self.until is None or entry.timestamp < self.until))


def _turns(entries: list[_Entry]) -> list[_Turn]:
    """Ходы по репликам пользователя основного журнала каждого сеанса; записи субагентов
    попадают в ход своего сеанса по времени."""
    turns: list[_Turn] = []
    for session in dict.fromkeys(entry.session for entry in entries):
        main = [entry for entry in entries if entry.session == session and not entry.subagent]
        if not main:
            continue
        starts = [main[0].timestamp] + [entry.timestamp for entry in main[1:] if entry.kind == "user"]
        for index, start in enumerate(starts):
            until = starts[index + 1] if index + 1 < len(starts) else None
            turns.append(_Turn(session, start, until))
    for entry in entries:
        for turn in turns:
            if turn.holds(entry):
                turn.entries.append(entry)
                break
    return turns


def _cut_window(log: SessionLog, entries: list[_Entry], root: str) -> None:
    def of_run(entry: _Entry) -> bool:
        call = entry.call
        return (entry.kind == "call" and not entry.subagent and call is not None
                and call.name == SHELL_TOOL and "alla_skill.py" in command_of(call)
                and not REVIEW_COMMAND_RE.search(command_of(call)) and root in call.result)

    turns = [turn for turn in _turns(entries) if any(of_run(entry) for entry in turn.entries)]
    if not turns:
        log.note = "в журнале нет команд этого разбора"
        return
    turns.sort(key=lambda turn: turn.start)
    window = [entry for turn in turns for entry in turn.entries]
    done = [entry for entry in window
            if of_run(entry) and entry.call is not None and DONE_RE.search(entry.call.result)]
    log.reached_done = bool(done)
    if done:
        # Финальный ответ — текст основного агента после последнего done до следующего
        # вызова или конца хода.
        last = done[-1]
        after = [entry for entry in window if entry.session == last.session
                 and not entry.subagent and entry.timestamp > last.timestamp]
        parts: list[str] = []
        for entry in after:
            if entry.kind in ("call", "user"):
                break
            if entry.kind == "text":
                parts.append(entry.text)
        log.final = "".join(parts)
    log.turns = len(turns)
    log.started = turns[0].start
    log.finished = max(turn.entries[-1].timestamp for turn in turns)
    spans = [_seconds(turn.start, turn.entries[-1].timestamp) for turn in turns]
    log.duration_seconds = None if None in spans else sum(span or 0 for span in spans)
    for entry in window:
        if entry.kind == "call" and entry.call is not None:
            entry.call.index = len(log.calls)
            log.calls.append(entry.call)
        elif entry.kind == "usage":
            log.usage.add(entry.usage)
        elif entry.kind == "api_error":
            log.api_errors += 1
    log.subagents = sum(1 for call in log.calls if call.name == "agent" and not call.subagent)
    log.skill_problems = _skill_problems(log)


def _seconds(start: str, end: str) -> int | None:
    begin, finish = _parse_time(start), _parse_time(end)
    if begin is None or finish is None:
        return None
    return max(0, int((finish - begin).total_seconds()))


def _skill_problems(log: SessionLog) -> list[str]:
    """Пункты блока «Проблемы скилла» финального ответа и строки субагентов."""
    problems: list[str] = []
    block = log.final.partition(PROBLEMS_HEADING)[2]
    for line in block.splitlines():
        match = re.match(r"\s*\d+\.\s+(.+)", line)
        if match:
            problems.append(match.group(1).strip())
    for call in log.calls:
        if call.name == "agent" and not call.subagent:
            for line in call.result.splitlines():
                if line.strip().startswith(SUBAGENT_PROBLEM):
                    problems.append(line.strip()[len(SUBAGENT_PROBLEM):].strip())
    return problems


def skill_status(call: ToolCall) -> str | None:
    """Статус ответа команды скилла; None — ответа без ``STATUS:`` (traceback и т. п.)."""
    match = _STATUS_RE.search(call.result)
    return match.group(1) if match else None


def skill_subcommand(call: ToolCall) -> str:
    """Имя команды скилла: ``prepare``, ``next``…; ``?`` — не распознана."""
    match = re.search(r"alla_skill\.py\S*\s+([a-z]+)", command_of(call))
    return match.group(1) if match else "?"


# --- разбор записей -----------------------------------------------------------------------

def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("Журнал %s не прочитан: %s", path, exc)
        return
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _parts(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def _result_text(call: ToolCall, response: Any, display: Any) -> str:
    """Текст результата. У shell — чистый вывод команды, без строк ``Command:``/``Exit Code:``
    журнала: так же его видит стенд в stream-json, и ``^STATUS:`` узнаётся."""
    if call.name == SHELL_TOOL and isinstance(display, dict) and isinstance(display.get("output"), str):
        return str(display["output"])
    if isinstance(response, dict):
        text = response.get("output", response.get("error", ""))
    else:
        text = response
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    if call.name == SHELL_TOOL:
        match = _SHELL_OUTPUT_RE.search(text)
        if match:
            return match.group(1)
    return text


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
