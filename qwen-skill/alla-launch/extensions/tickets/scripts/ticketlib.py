"""Ticket workflow: source-owned documents, immutable reviews and durable requests.

This module deliberately does not import the skill's implementation. Its only data
interface is the documented extension snapshot command.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

API = 1
ACTIVE = {"ready", "sending", "unknown"}
FINAL = {"created", "linked"}
CATEGORIES = {"приложение": "application", "тест": "test", "окружение": "environment",
              "данные": "data", "неизвестно": "unknown"}
CONTROL = re.compile(r"^(team|kind|operation|target)_(\d+)$")
TEXT_CONTROL = re.compile(r"^(question|check)_(\d+)$")
SECRETS = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer|basic)\s+\S+|"
    r"(?:password|api[_-]?key|access[_-]?token)\s*[=:]\s*[^\s,;]{6,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


class TicketError(Exception):
    """Safe message for the engineer; adapter stderr is never interpolated here."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"))


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory)
        sync_directory(directory.parent)


def atomic(path: Path, value: Any) -> None:
    ensure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def identifier(value: str) -> str:
    try:
        result = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise TicketError("Нужен сохранённый UUID запроса или решения.") from exc
    if result != value:
        raise TicketError("UUID должен иметь канонический вид.")
    return result


def context() -> dict[str, Any]:
    value = read_json(Path(os.environ["ALLA_CONTEXT"]))
    if not isinstance(value, dict):
        raise TicketError("Контекст расширения повреждён.")
    return value


class Store:
    def __init__(self, ctx: dict[str, Any]) -> None:
        self.context = ctx
        self.root = Path(ctx["state_dir"])
        self.project = str(Path(ctx["project_root"]).resolve())
        ensure_directory(self.root)
        self.requests = self.root / "requests"
        ensure_directory(self.requests)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with (self.root / "tickets.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.context["_lease_fd"] = handle.fileno()
            try:
                yield
            finally:
                # No LOCK_UN: an orphaned adapter may still own this same open-file
                # description. Its inherited descriptor keeps the lease until exit.
                self.context.pop("_lease_fd", None)

    def groups(self) -> list[dict[str, Any]]:
        path = self.root / "groups.json"
        return list(read_json(path)) if path.is_file() else []

    def records(self) -> list[dict[str, Any]]:
        return [self.load(path.stem) for path in sorted(self.requests.glob("*.json"))]

    def load(self, request_id: str) -> dict[str, Any]:
        path = self.requests / f"{identifier(request_id)}.json"
        if not path.is_file():
            raise TicketError("Запрос не найден в журнале этого проекта.")
        record = read_json(path)
        if (not isinstance(record, dict) or record.get("schema") != 1
                or record.get("extension") != "tickets"
                or record.get("request_id") != request_id
                or record.get("project_root") != self.project):
            raise TicketError("Запись запроса не принадлежит этому проекту или повреждена.")
        return record

    def save(self, record: dict[str, Any]) -> None:
        atomic(self.requests / f"{identifier(record['request_id'])}.json", record)


def snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    command = ctx.get("snapshot_command")
    if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command):
        raise TicketError("В контексте нет команды получения актуального снимка.")
    try:
        completed = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL,
                                   timeout=20, check=False)
        first, _, body = completed.stdout.decode("utf-8").partition("\n")
        if completed.returncode != 0 or first.strip() != "STATUS: ok":
            raise TicketError("Не удалось получить актуальный снимок разбора.")
        exported = json.loads(body)
        result = read_json(Path(exported["path"]))
        if not isinstance(result, dict) or result.get("run_uid") != ctx.get("run_uid"):
            raise TicketError("Снимок относится к другому разбору.")
        return result
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        raise TicketError("Не удалось прочитать актуальный снимок разбора.") from exc


def problems(snap: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(item["number"]): item for item in snap.get("problems", [])}


def diagnosis(items: dict[int, dict[str, Any]], numbers: list[int]) -> str:
    return digest([{key: items.get(number, {}).get(key)
                    for key in ("category", "cause", "what", "fix")}
                   for number in sorted(numbers)])


def string_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return ""


def quoted_span(quote: str, text: str) -> str:
    """Accept core quote abbreviations, export only actual bytes from the source."""
    if quote in text:
        return quote
    translations = str.maketrans({char: '"' for char in "«»“”„‘’`'"})

    def normalized(value: str) -> str:
        return " ".join(value.translate(translations).casefold().split())

    haystack = ""
    positions: list[int] = []
    for index, char in enumerate(text):
        folded = char.translate(translations).casefold()
        if folded.isspace():
            if haystack and not haystack.endswith(" "):
                haystack += " "
                positions.append(index)
        else:
            haystack += folded
            positions.extend([index] * len(folded))
    parts = [normalized(part).strip(" .,;:") for part in re.split(r"…|\.{3,}", quote)]
    parts = [part for part in parts if part]
    if not parts:
        raise TicketError("Пустая цитата не подтверждает наблюдение.")
    offset, start = 0, -1
    for part in parts:
        found = haystack.find(part, offset)
        if found < 0:
            raise TicketError("Цитата не найдена в источнике.")
        if start < 0:
            start = found
        offset = found + len(part)
    return text[positions[start]:positions[offset - 1] + 1]


def evidence(problem: dict[str, Any]) -> list[dict[str, Any]]:
    sources = {str(row["id"]): row for row in problem.get("sources", [])}
    result = []
    for row in problem.get("observations", []):
        source_id = str(row.get("source_id", ""))
        quote = str(row.get("quote", ""))
        source = sources.get(source_id)
        if not source or not quote:
            raise TicketError(f"Проблема {problem['number']}: наблюдение без источника.")
        text = str(source.get("text", ""))
        quote = quoted_span(quote, text)
        result.append({"problem": int(problem["number"]), "source_id": source_id,
                       "title": str(source.get("title", "")), "quote": quote})
    if not result:
        raise TicketError(f"Проблема {problem['number']}: нет проверяемых наблюдений.")
    return result


def controls(ctx: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], list[list[int]]]:
    params: dict[str, str] = {}
    groups = []
    for key, values in ctx.get("params", {}).items():
        if key == "group":
            for value in values:
                try:
                    numbers = [int(part.strip()) for part in value.split(",")]
                except ValueError as exc:
                    raise TicketError("group — номера через запятую.") from exc
                if len(numbers) < 2 or len(set(numbers)) != len(numbers):
                    raise TicketError("В group нужны минимум два разных номера.")
                groups.append(sorted(numbers))
        else:
            if key != "revoke_group" and not CONTROL.fullmatch(key):
                raise TicketError(f"Неизвестный управляющий параметр: {key}.")
            if len(values) != 1:
                raise TicketError(f"Параметр {key} должен быть указан один раз.")
            params[key] = str(values[0])
    text: dict[str, str] = {}
    for key, value in ctx.get("text_params", {}).items():
        if not TEXT_CONTROL.fullmatch(key) or not isinstance(value, str):
            raise TicketError("Текстовые входы — только question_N и check_N.")
        if not value.strip() or len(value) > 16_000 or "\x00" in value:
            raise TicketError(f"Поле {key} пустое, слишком длинное или содержит NUL.")
        text[key] = value
    return params, text, groups


def active_groups(store: Store, snap: dict[str, Any]) -> list[dict[str, Any]]:
    items = problems(snap)
    return [row for row in store.groups()
            if row["run_uid"] == snap["run_uid"] and not row.get("revoked")
            and all(number in items for number in row["numbers"])
            and row["diagnosis_hash"] == diagnosis(items, row["numbers"])]


def group_label(basis: dict[str, Any]) -> str:
    decision = basis.get("decision_id")
    suffix = f"; сохранённое решение {decision}" if decision else f"; {basis['source']}"
    return f"{basis['numbers']}{suffix}"


def assess(ctx: dict[str, Any], snap: dict[str, Any], store: Store,
           *, automatic: bool = False) -> dict[str, Any]:
    params, text, explicit = controls({} if automatic else ctx)
    items = problems(snap)
    selected = sorted(items) if automatic else sorted(set(ctx.get("selected_numbers", [])))
    if not selected:
        raise TicketError("Назови номера проблем для подготовки тикетов.")
    rejected: dict[int, str] = {}
    research: set[int] = set()
    if automatic:
        for record in store.records():
            if record["run_uid"] != snap["run_uid"] or record["state"] == "superseded":
                continue
            ticket = record["version"]["ticket"]
            if ticket["kind"] != "investigation" or not ticket.get("question") or not ticket.get("check"):
                continue
            prior = {row["number"]: row for row in record["version"]["projection"]["problems"]}
            for number in record["numbers"]:
                if number in items and all(prior[number].get(key) == items[number].get(key)
                                           for key in ("category", "cause", "what", "fix")):
                    research.add(number)
    for number in selected:
        problem = items.get(number)
        if not problem or not problem.get("valid"):
            rejected[number] = "нет принятого разбора"
            continue
        try:
            evidence(problem)
        except TicketError as exc:
            rejected[number] = str(exc)
            continue
        if automatic and problem.get("has_fix"):
            rejected[number] = "есть готовая правка автотеста"
        if automatic and problem.get("category") == "неизвестно" and number not in research:
            rejected[number] = "для исследования нужны вопрос и проверка инженера"
    valid = set(selected) - set(rejected)
    for group in explicit:
        if not set(group) <= set(selected) or not set(group) <= valid:
            raise TicketError(f"Группа {group}: есть неназванный или отклонённый номер; "
                              "состав не обрезается.")
    saved = active_groups(store, snap)
    bases = [{"numbers": sorted(row["numbers"]), "cause": row.get("cause", ""),
              "source": "report"} for row in snap.get("groups", [])]
    bases += [{**row, "source": "engineer"} for row in saved]
    bases += [{"numbers": group, "source": "explicit"} for group in explicit]
    # Follow intersections only to detect conflicting bases, never to merge them.
    scope = set(selected)
    changed = True
    while changed:
        before = len(scope)
        for base in bases:
            if scope.intersection(base["numbers"]):
                scope.update(base["numbers"])
        changed = len(scope) != before
    relevant = [base for base in bases if scope.intersection(base["numbers"])]
    for position, base in enumerate(relevant):
        for other in relevant[position + 1:]:
            if set(base["numbers"]) & set(other["numbers"]) and (
                    base["numbers"] != other["numbers"]):
                raise TicketError(f"Конфликт оснований группировки: {group_label(base)} и "
                                  f"{group_label(other)}. Сначала отзови сохранённое решение "
                                  "или уточни состав. Версии не сохранены.")
    unique: dict[tuple[int, ...], dict[str, Any]] = {}
    rank = {"report": 0, "engineer": 1, "explicit": 2}
    for base in relevant:
        key = tuple(base["numbers"])
        if key not in unique or rank[base["source"]] > rank[unique[key]["source"]]:
            unique[key] = base
    units: list[dict[str, Any]] = []
    covered: set[int] = set()
    for base in unique.values():
        numbers = sorted(valid.intersection(base["numbers"]))
        if numbers:
            units.append({"numbers": numbers, "basis": base if len(numbers) > 1 else None})
            covered.update(numbers)
    units += [{"numbers": [number], "basis": None} for number in sorted(valid - covered)]
    units.sort(key=lambda unit: unit["numbers"][0])
    anchors = {unit["numbers"][0] for unit in units}
    for control_key in [*params, *text]:
        if control_key == "revoke_group":
            continue
        if int(control_key.rsplit("_", 1)[1]) not in anchors:
            raise TicketError(f"Поле {control_key} не относится к получившемуся тикету.")
    return {"units": units, "rejected": rejected, "params": params, "text": text}


def revoke(ctx: dict[str, Any], store: Store) -> str | None:
    params, text, groups = controls(ctx)
    decision = params.get("revoke_group")
    if not decision:
        return None
    if len(params) != 1 or text or groups or ctx.get("selected_numbers"):
        raise TicketError("Отзыв группы — отдельная команда без номеров и других параметров.")
    identifier(decision)
    rows = store.groups()
    row = next((item for item in rows if item["decision_id"] == decision
                and item["run_uid"] == ctx["run_uid"]), None)
    if row is None:
        raise TicketError("Решение этой группы в текущем разборе не найдено.")
    row["revoked"] = True
    atomic(store.root / "groups.json", rows)
    return f"Решение {decision} отозвано. Тикеты не создавались; отправки не отменялись."


def effective_settings(settings: dict[str, Any], category: str,
                       team_override: bool) -> dict[str, Any]:
    keys = ["adapter_command", "adapter_env", "adapter_timeout"]
    if not team_override:
        keys.append(f"team_{CATEGORIES.get(category, 'unknown')}")
    return {key: settings.get(key) for key in keys}


def make_ticket(ctx: dict[str, Any], snap: dict[str, Any], unit: dict[str, Any],
                assessment: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    items = problems(snap)
    numbers = unit["numbers"]
    anchor = numbers[0]
    selected = [items[number] for number in numbers]
    categories = {row["category"] for row in selected}
    if len(categories) != 1:
        raise TicketError("У общей группы разные категории диагноза; уточни разбор.")
    category = str(selected[0]["category"])
    params, text = assessment["params"], assessment["text"]
    kind = params.get(f"kind_{anchor}", "investigation" if category == "неизвестно" else
                      "bug" if category == "приложение" else "task")
    if kind not in {"bug", "task", "investigation"}:
        raise TicketError("Тип тикета — bug, task или investigation.")
    action = params.get(f"operation_{anchor}", "create")
    if action not in {"create", "link"}:
        raise TicketError("Операция — create или link.")
    target = params.get(f"target_{anchor}")
    if (action == "link") != bool(target):
        raise TicketError("Для link нужен target_N, для create он недопустим.")
    settings = snap.get("settings", ctx.get("settings", {}))
    team = params.get(f"team_{anchor}", settings.get(f"team_{CATEGORIES.get(category, 'unknown')}", ""))
    if not isinstance(team, str) or not team.strip():
        raise TicketError(f"Для тикета {anchor} укажи team_{anchor} или маршрут категории.")
    question, check = text.get(f"question_{anchor}"), text.get(f"check_{anchor}")
    if kind == "investigation" and (not question or not check):
        raise TicketError(f"Для исследования {anchor} нужны question_{anchor} и check_{anchor} "
                          "через --text-file.")
    if kind != "investigation" and (question or check):
        raise TicketError("Вопрос и проверка допустимы только для investigation.")
    proofs = [proof for row in selected for proof in evidence(row)]
    tests = []
    for row in selected:
        for test in row.get("tests", []):
            entry = {key: test.get(key) for key in ("name", "full_name", "link")}
            if entry not in tests:
                tests.append(entry)
    description = []
    for row in selected:
        description.extend([f"Проблема {row['number']}: {row.get('what') or row['label']}",
                            f"Диагноз ({'подтверждён инженером' if row.get('confirmed') else 'вывод разбора'}): "
                            f"{row['category']} — {row['cause']}",
                            f"Предлагаемые действия: {string_value(row.get('fix'))}"])
    description += [f"[{p['problem']}:{p['source_id']}] {p['title']}\n{p['quote']}" for p in proofs]
    description += [f"Тест: {row.get('full_name') or row.get('name')} {row.get('link') or ''}"
                    for row in tests]
    if question and check:
        description += [f"Вопрос инженера:\n{question}", f"Проверка инженера:\n{check}"]
    title = str(selected[0].get("what") or selected[0]["label"]).replace("\n", " ")[:180]
    ticket = {"kind": kind, "team": team, "title": title,
              "description": "\n\n".join(description), "problems": numbers, "tests": tests,
              "evidence": proofs, "launch_id": snap.get("launch_id"),
              "run_uid": snap["run_uid"]}
    if question and check:
        ticket.update(question=question, check=check)
    if SECRETS.search(canonical(ticket).decode("utf-8")):
        raise TicketError("В данных тикета найден возможный секрет; отправка запрещена.")
    team_override = f"team_{anchor}" in params
    projected = []
    for row in selected:
        item = {key: row.get(key) for key in (
            "number", "category", "cause", "fix", "what", "missing", "confirmed",
            "valid", "tests", "observations", "has_fix", "rerun")}
        cited = {proof["source_id"] for proof in proofs if proof["problem"] == row["number"]}
        item["sources"] = [{"id": source["id"], "sha256": digest(source)}
                           for source in row.get("sources", []) if source["id"] in cited]
        projected.append(item)
    projection = {"run_uid": snap["run_uid"], "numbers": numbers,
                  "problems": projected,
                  "basis": unit["basis"],
                  "settings": effective_settings(settings, category, team_override),
                  "controls": {"kind": kind, "team": team, "operation": action,
                               "target_id": target, "question": question, "check": check,
                               "team_override": team_override}}
    return ticket, projection


class Adapter:
    def __init__(self, ctx: dict[str, Any], settings: dict[str, Any],
                 *, allow_unconfigured: bool = False) -> None:
        self.lease_fd = ctx.get("_lease_fd")
        command = settings.get("adapter_command", [])
        self.configured = bool(command)
        if not command and allow_unconfigured:
            self.command = []
            self.deadline = time.monotonic() + 90
            return
        if not isinstance(command, list) or not command or not all(
                isinstance(item, str) and item for item in command):
            raise TicketError("Адаптер не подключён: задай adapter_command.")
        project = Path(ctx["project_root"]).resolve()
        program = Path(command[0])
        if program.is_absolute() or ".." in program.parts or len(program.parts) < 2:
            raise TicketError("Первый элемент adapter_command — путь от корня проекта, без PATH.")
        self.command = [str(project / program), *command[1:]]
        allowed = settings.get("adapter_env", [])
        if not isinstance(allowed, list) or not all(isinstance(key, str) for key in allowed):
            raise TicketError("adapter_env — список имён переменных.")
        if any(key.startswith(("ALLURE_", "ALLA_", "QWEN_")) or key == "PATH" for key in allowed):
            raise TicketError("Окружение TestOps/Qwen/PATH адаптеру не передаётся.")
        self.env = {key: os.environ[key] for key in allowed if key in os.environ}
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.project = project
        self.timeout = min(30, max(1, int(settings.get("adapter_timeout", 25))))
        self.deadline = time.monotonic() + 90

    def call(self, operation: str, **data: Any) -> dict[str, Any]:
        if not self.configured:
            if operation == "prepare":
                return {"api": API, "scope": {"site": "unconfigured", "project_or_queue": "draft"},
                        "capabilities": {"search": False, "authoritative_absence": False},
                        "payload": base64.b64encode(canonical(data["ticket"])).decode("ascii")}
            raise TicketError("Адаптер не подключён; доступны только черновики.")
        remaining = min(self.timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TicketError("Исчерпан срок команды адаптера.")
        try:
            inherited = (self.lease_fd,) if isinstance(self.lease_fd, int) else ()
            result = subprocess.run(self.command, cwd=self.project, env=self.env,
                                    input=canonical({"api": API, "operation": operation, **data}),
                                    capture_output=True, timeout=remaining, check=False,
                                    pass_fds=inherited)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TicketError("Адаптер не завершил операцию; подробности не выводятся.") from exc
        if result.returncode != 0 or len(result.stdout) > 2_000_000:
            raise TicketError("Адаптер не вернул подтверждённый результат.")
        try:
            response = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise TicketError("Stdout адаптера должен содержать только JSON протокола.") from exc
        if not isinstance(response, dict) or response.get("api") != API or response.get("error"):
            raise TicketError("Ответ адаптера не соответствует протоколу или содержит ошибку.")
        return response


def prepared(adapter: Adapter, ticket: dict[str, Any], action: str, target: str | None,
             request_id: str) -> dict[str, Any]:
    response = adapter.call("prepare", ticket=ticket, action=action, target_id=target,
                            request_id=request_id)
    scope = response.get("scope")
    caps = response.get("capabilities")
    if (not isinstance(scope, dict) or set(scope) != {"site", "project_or_queue"}
            or not all(isinstance(value, str) and value for value in scope.values())
            or not isinstance(caps, dict)
            or not isinstance(caps.get("search"), bool)
            or not isinstance(caps.get("authoritative_absence"), bool)):
        raise TicketError("prepare должен вернуть устойчивую область и возможности адаптера.")
    payload = response.get("payload")
    if not isinstance(payload, str):
        raise TicketError("prepare должен вернуть payload в base64.")
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise TicketError("prepare должен вернуть конечные байты payload в base64.") from exc
    if not decoded or len(decoded) > 1_000_000:
        raise TicketError("Нагрузка адаптера пустая или слишком большая.")
    try:
        payload_text = decoded.decode("utf-8")
    except UnicodeError as exc:
        raise TicketError("Нагрузка должна быть UTF-8 для полного просмотра.") from exc
    if SECRETS.search(payload_text):
        raise TicketError("В конечной нагрузке найден возможный секрет.")
    return {"scope": scope, "capabilities": {"search": caps["search"],
             "authoritative_absence": caps["authoritative_absence"]}, "payload": payload}


def search(adapter: Adapter, prepare: dict[str, Any], ticket: dict[str, Any],
           action: str, target: str | None, request_id: str) -> dict[str, Any]:
    if not prepare["capabilities"]["search"]:
        if action == "link":
            raise TicketError("Без поиска связывание недоступно.")
        return {"status": "unsupported", "matches": []}
    result = adapter.call("search", scope=prepare["scope"], ticket=ticket, action=action,
                          target_id=target, request_id=request_id)
    rows = result.get("matches")
    if not isinstance(rows, list):
        raise TicketError("Поиск не вернул список совпадений; версия не фиксируется.")
    normal = []
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                                               for key in ("id", "url", "state", "reason")):
            raise TicketError("Некорректное совпадение поиска; версия не фиксируется.")
        entry = {key: row[key] for key in ("id", "url", "state", "reason")}
        if entry not in normal:
            normal.append(entry)
    normal.sort(key=lambda row: (row["id"], row["url"], row["state"], row["reason"]))
    if action == "link" and target not in {row["id"] for row in normal}:
        raise TicketError("Целевой тикет отсутствует в текущем результате поиска.")
    return {"status": "supported", "matches": normal}


def committed_basis(store: Store, snap: dict[str, Any], basis: Any) -> Any:
    if not basis or basis["source"] != "explicit":
        return basis
    numbers = basis["numbers"]
    existing = next((row for row in active_groups(store, snap) if row["numbers"] == numbers), None)
    row = existing or {"decision_id": str(uuid.uuid4()), "run_uid": snap["run_uid"],
                       "numbers": numbers, "diagnosis_hash": diagnosis(problems(snap), numbers),
                       "revoked": False}
    return {**row, "source": "engineer"}


def draft(ctx: dict[str, Any], anchor: int, store: Store) -> str:
    snap = snapshot(ctx)
    assessment = assess(ctx, snap, store)
    unit = next((row for row in assessment["units"] if row["numbers"][0] == anchor), None)
    if not unit:
        raise TicketError("Такого тикета в текущем плане нет.")
    records = store.records()
    overlapping = [record for record in records if record["run_uid"] == snap["run_uid"]
                   and set(record["numbers"]) & set(unit["numbers"])]
    for record in overlapping:
        if record["state"] in {"sending", "unknown"}:
            return f"Запрос {record['request_id']}: результат неизвестен. Выполни reconcile; новая отправка запрещена."
        if record["state"] in FINAL and record["numbers"] == unit["numbers"]:
            return receipt_text(record)
    completed = [record for record in overlapping if record["state"] in FINAL]
    if completed:
        covered = {number for record in completed for number in record["numbers"]}
        uncovered = sorted(set(unit["numbers"]) - covered)
        existing = [record["numbers"] for record in completed]
        raise TicketError(f"Частичное пересечение с выполненными тикетами: {existing}; "
                          f"выбранный состав {unit['numbers']}; ещё не обработаны {uncovered}. "
                          "Подготовь оставшиеся номера отдельно или явно свяжи их с найденным тикетом.")
    ticket, projection = make_ticket(ctx, snap, unit, assessment)
    projection["basis"] = committed_basis(store, snap, projection["basis"])
    settings = snap.get("settings", ctx["settings"])
    request_id = str(uuid.uuid4())  # provisional: neither reservation nor journal entry yet
    action = projection["controls"]["operation"]
    target = projection["controls"]["target_id"]
    adapter = Adapter(ctx, settings, allow_unconfigured=True)
    ready = prepared(adapter, ticket, action, target, request_id)
    matches = search(adapter, ready, ticket, action, target, request_id)
    version = {"ticket": ticket, "projection": projection, "prepared": ready, "search": matches}
    # Ignore only the provisional correlation ID when recognising a repeated preparation:
    # native idempotency payloads containing the ID reuse the original candidate below.
    for previous in overlapping:
        if previous["state"] != "ready" or previous["numbers"] != unit["numbers"]:
            continue
        old = previous["version"]
        if old["ticket"] == ticket and old["projection"] == projection:
            prior_ready = prepared(adapter, ticket, action, target, previous["request_id"])
            prior_search = search(adapter, prior_ready, ticket, action, target, previous["request_id"])
            prior_version = {"ticket": ticket, "projection": projection,
                             "prepared": prior_ready, "search": prior_search}
            if prior_version == old:
                return f"Черновик уже сохранён: {previous['request_id']}. Покажи его действием show."
    for previous in overlapping:
        if previous["state"] == "ready":
            previous["state"] = "superseded"
            store.save(previous)
    record = {"schema": 1, "extension": "tickets", "request_id": request_id,
              "project_root": store.project, "run_uid": snap["run_uid"],
              "run_dir": ctx["run_dir"], "numbers": unit["numbers"], "operation": action,
              "state": "ready", "version": version, "version_hash": digest(version),
              "shown_hash": None, "created": time.time()}
    basis = projection["basis"]
    if basis and basis["source"] == "engineer":
        groups = store.groups()
        if not any(row["decision_id"] == basis["decision_id"] for row in groups):
            groups.append({key: value for key, value in basis.items() if key != "source"})
            atomic(store.root / "groups.json", groups)
    store.save(record)
    return f"Черновик сохранён: {request_id}. Покажи его действием show."


def checked_version(record: dict[str, Any]) -> dict[str, Any]:
    version = record.get("version")
    if not isinstance(version, dict) or digest(version) != record.get("version_hash"):
        raise TicketError("Байты сохранённой версии изменились; отправка запрещена.")
    if (version["ticket"]["problems"] != record["numbers"]
            or version["projection"]["run_uid"] != record["run_uid"]
            or version["projection"]["controls"]["operation"] != record["operation"]):
        raise TicketError("Состав или операция запроса изменились.")
    return version


def show(record: dict[str, Any], store: Store, ctx: dict[str, Any] | None = None) -> str:
    version = checked_version(record)
    path = store.root / f"{record['request_id']}.review.md"
    ticket = version["ticket"]
    search_note = "Поиск не поддерживается адаптером." if version["search"]["status"] == "unsupported" else (
        json.dumps(version["search"]["matches"], ensure_ascii=False, indent=2))
    payload = base64.b64decode(version["prepared"]["payload"]).decode("utf-8", "strict")
    text = (f"# {ticket['title']}\n\nОперация: {record['operation']}\n\n"
            f"Проблемы: {record['numbers']}\n\nКоманда: {ticket['team']}\n\n"
            f"Основание группировки: {json.dumps(version['projection']['basis'], ensure_ascii=False)}\n\n"
            f"Адресат: {json.dumps(version['prepared']['scope'], ensure_ascii=False)}\n\n"
            f"Целевой тикет: {version['projection']['controls']['target_id']}\n\n"
            f"{ticket['description']}\n\n## Поиск похожих тикетов\n\n{search_note}\n\n"
            f"## Все отправляемые байты UTF-8\n\n```\n{payload}\n```\n")
    path.write_bytes(text.encode("utf-8"))
    record["shown_hash"] = record["version_hash"]
    record["review_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    store.save(record)
    output = (f"Полный просмотр: {path}\nЗапрос: {record['request_id']}\n"
              f"Хеш показанной версии: {record['shown_hash']}\n"
              "Основной агент: прочитай и передай инженеру полный файл; дождись его ответа. "
              "Субагент: закончи работу, submit не вызывай.")
    if ctx and ctx.get("snapshot_command"):
        context_path = ctx.get("context_path") or os.environ.get("ALLA_CONTEXT")
        if context_path:
            command = [*ctx["snapshot_command"][:2], "extensions", "action", "tickets", "submit",
                       "--context", str(context_path)]
            if ctx.get("reports_dir"):
                command += ["--reports-dir", str(ctx["reports_dir"])]
            command += ["--", "--request", record["request_id"], "--shown", record["shown_hash"]]
            output += "\nТолько основной агент после явного одобрения показанной версии:\n" + shlex.join(command)
    return output


def receipt_text(record: dict[str, Any]) -> str:
    receipt = record.get("receipt", {})
    verb = "Создан тикет" if record["state"] == "created" else "Связь добавлена в тикет"
    return f"{verb}: {receipt.get('id', '')} {receipt.get('url', '')}. Запрос {record['request_id']}."


def validate_receipt(response: dict[str, Any]) -> dict[str, str]:
    ticket = response.get("ticket")
    if not isinstance(ticket, dict) or not all(
            isinstance(ticket.get(key), str) and ticket[key].strip()
            and len(ticket[key]) <= 1000 and not any(ord(char) < 32 for char in ticket[key])
            for key in ("id", "url", "state")):
        raise TicketError("Адаптер не подтвердил идентификатор и ссылку результата.")
    return {key: ticket[key] for key in ("id", "url", "state")}


def reconcile(ctx: dict[str, Any], record: dict[str, Any], store: Store) -> str:
    if record["state"] in FINAL:
        return receipt_text(record)
    if record["state"] not in {"sending", "unknown"}:
        return f"Запрос {record['request_id']}: {record['state']}. Внешняя отправка не выполнялась."
    version = checked_version(record)
    try:
        adapter = Adapter(ctx, ctx["settings"])
        current = prepared(adapter, version["ticket"], record["operation"],
                           version["projection"]["controls"]["target_id"], record["request_id"])
        if current["scope"] != version["prepared"]["scope"]:
            raise TicketError("Изменилась область трекера.")
        response = adapter.call("lookup", action=record["operation"], request_id=record["request_id"],
                                scope=version["prepared"]["scope"])
        outcome = response.get("outcome")
        if outcome == "found":
            record["receipt"] = validate_receipt(response)
            record["state"] = "created" if record["operation"] == "create" else "linked"
        elif (outcome == "absent" and version["prepared"]["capabilities"]["authoritative_absence"]
              and current["capabilities"]["authoritative_absence"]):
            record["state"] = "ready"
        else:
            record["state"] = "unknown"
    except TicketError:
        record["state"] = "unknown"
    store.save(record)
    if record["state"] in FINAL:
        return receipt_text(record)
    if record["state"] == "ready":
        return (f"Операции точно не было. Запрос {record['request_id']} открыт для отдельного "
                "submit той же версии после проверки актуальности. Отправка не выполнялась.")
    return f"Запрос {record['request_id']}: итог неизвестен; пересекающиеся проблемы заблокированы."


def submit(ctx: dict[str, Any], record: dict[str, Any], shown: str, store: Store) -> str:
    if record["state"] in FINAL:
        return receipt_text(record)
    if record["state"] in {"sending", "unknown"}:
        return reconcile(ctx, record, store)
    version = checked_version(record)
    if record["state"] != "ready" or not shown or (
            shown != record["shown_hash"] or shown != record["version_hash"]):
        raise TicketError("Нужна текущая показанная версия и её хеш.")
    review = store.root / f"{record['request_id']}.review.md"
    if (not review.is_file() or hashlib.sha256(review.read_bytes()).hexdigest()
            != record.get("review_hash")):
        raise TicketError("Файл полного просмотра изменён; выполни show заново.")
    snap = snapshot(ctx)
    if snap["run_uid"] != record["run_uid"]:
        raise TicketError("Запрос относится к другому разбору.")
    original = version["projection"]
    # Rebuild from the frozen user controls, not newly supplied text or model output.
    frozen_ctx = {**ctx, "selected_numbers": record["numbers"], "params": {}, "text_params": {}}
    anchor = record["numbers"][0]
    for key, prefix in (("kind", "kind"), ("team", "team"), ("operation", "operation"),
                        ("target_id", "target")):
        if key == "team" and not original["controls"].get("team_override"):
            continue
        value = original["controls"].get(key)
        if value is not None:
            frozen_ctx["params"][f"{prefix}_{anchor}"] = [value]
    for key in ("question", "check"):
        value = original["controls"].get(key)
        if value is not None:
            frozen_ctx["text_params"][f"{key}_{anchor}"] = value
    assessment = assess(frozen_ctx, snap, store)
    unit = next((row for row in assessment["units"] if row["numbers"] == record["numbers"]), None)
    if unit is None:
        raise TicketError("Состав или основание группы изменились; нужен новый просмотр.")
    ticket, projection = make_ticket(ctx, snap, unit, assessment)
    if ticket != version["ticket"] or projection != original:
        raise TicketError("Проекция проблем или настройки изменились; нужен новый просмотр.")
    adapter = Adapter(ctx, snap.get("settings", ctx["settings"]))
    ready = prepared(adapter, ticket, record["operation"], original["controls"]["target_id"],
                     record["request_id"])
    matches = search(adapter, ready, ticket, record["operation"], original["controls"]["target_id"],
                     record["request_id"])
    if ready != version["prepared"] or matches != version["search"]:
        raise TicketError("Адресат, нагрузка, возможности или поиск изменились; нужен новый show.")
    for other in store.records():
        if (other["request_id"] != record["request_id"] and other["run_uid"] == record["run_uid"]
                and set(other["numbers"]) & set(record["numbers"])
                and other["state"] in ACTIVE | FINAL):
            raise TicketError("Есть другая активная или выполненная отправка этих проблем.")
    record["state"] = "sending"
    store.save(record)  # durable before the adapter process can mutate anything
    try:
        response = adapter.call(record["operation"], request_id=record["request_id"],
                                scope=version["prepared"]["scope"],
                                payload=version["prepared"]["payload"])
        if response.get("outcome") != "found":
            raise TicketError("Нет подтверждения результата.")
        record["receipt"] = validate_receipt(response)
        record["state"] = "created" if record["operation"] == "create" else "linked"
    except TicketError:
        record["state"] = "unknown"
    store.save(record)
    return receipt_text(record) if record["state"] in FINAL else (
        f"Запрос {record['request_id']}: итог неизвестен. Повтор — только lookup, не новая отправка.")


def run(operation: str) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", type=int)
    parser.add_argument("--request")
    parser.add_argument("--shown", default="")
    args = parser.parse_args()
    try:
        ctx = context()
        store = Store(ctx)
        with store.lock():
            if operation == "hint":
                if not ctx.get("settings", {}).get("hint", True):
                    return 0
                snap = snapshot(ctx)
                result = assess({**ctx, "params": {}, "text_params": {}}, snap, store,
                                automatic=True)
                rows = [f"Проблемы {unit['numbers']}: имеет смысл подготовить тикет — "
                        f"/alla:ticket {' '.join(str(n) for n in unit['numbers'])}."
                        for unit in result["units"]]
                if rows:
                    print("\n".join(rows[:5]))
                    if len(rows) > 5:
                        print(f"Ещё кандидатов: {len(rows) - 5}.")
                return 0
            if operation == "plan":
                message = revoke(ctx, store)
                if message:
                    result_text = message
                else:
                    result = assess(ctx, snapshot(ctx), store)
                    result_text = "\n".join(
                        [f"Тикет {row['numbers'][0]}: проблемы {row['numbers']}"
                         + (f"; основание {group_label(row['basis'])}" if row['basis'] else "")
                         for row in result["units"]]
                        + [f"Отклонена проблема {key}: {value}" for key, value in result["rejected"].items()])
            elif operation == "draft":
                if args.unit is None:
                    raise TicketError("Нужен --unit с номером из плана.")
                result_text = draft(ctx, args.unit, store)
            else:
                request_id = args.request or ctx.get("request_id")
                if not request_id:
                    raise TicketError("Нужен --request с сохранённым UUID.")
                record = store.load(request_id)
                if operation == "show":
                    result_text = show(record, store, ctx)
                elif operation == "submit":
                    result_text = submit(ctx, record, args.shown, store)
                else:
                    result_text = reconcile(ctx, record, store)
        print("STATUS: ok")
        print(result_text)
        return 0
    except (TicketError, OSError, ValueError, KeyError, TypeError) as exc:
        if operation == "hint":
            print(f"Тикеты: рекомендации недоступны — {exc}")
            return 0
        print("STATUS: error")
        print(str(exc) if isinstance(exc, TicketError) else "Данные расширения повреждены или недоступны.")
        return 2
