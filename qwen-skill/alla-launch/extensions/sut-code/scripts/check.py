"""Действие ``check`` расширения sut-code: проверить разбор субагента и сохранить итог.

Субагент пишет разбор в ``<stem>.txt`` (формат — ``RESULT_FORMAT``, он же в README и
задании). Проверка сверяет его с кодом выбранных ревизий и с данными падения: место и
цитаты кода — в файле ревизии рядом с названной строкой, цитата лога — в куске ``[S…]``,
коммиты — в репозитории. Ошибки — ``STATUS: error`` с замечаниями (попытка N из 3);
третью версию принимаем с пометкой «формат нарушен».

Итог: ``<stem>.result.json`` (всё разобранное — вход будущих расширений, например заведения
дефекта), ``<stem>.report.md`` (для человека) и ``<stem>.finding.json`` — вывод по контракту
расширений: его скилл сохраняет в отчёт и базу знаний, когда инженер подтвердит
(``remember N --from-extension sut-code``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sutlib
from sutlib import SutError

MAX_ATTEMPTS = 3
QUOTE_RADIUS = 3  # на сколько строк цитата кода может отстоять от названной
TITLE_LIMIT = 120
RESULT_FORMAT = """\
РЕВИЗИЯ: <репозиторий> <ревизия> (<коммит, 8 знаков>)
ВЫВОД: <приложение|тест|окружение|данные|неизвестно> — <причина одной фразой>
МЕСТО: <путь>:<строка> — <что там происходит>
КОД:
- <путь>:<строка> «<дословная строка кода>»
СВЯЗЬ С ПАДЕНИЕМ: [S<номер>] «<дословная цитата из данных падения>» — <как она следует из кода>
ИСТОРИЯ: <коммит> <дата> — <что изменил> | нет данных
КАК ИСПРАВИТЬ:
1. <шаг>
ДЕФЕКТ: заводить | не заводить — <почему>
ЗАГОЛОВОК ДЕФЕКТА: <до 120 символов>"""
SECTIONS = ("РЕВИЗИЯ", "ВЫВОД", "МЕСТО", "КОД", "СВЯЗЬ С ПАДЕНИЕМ", "ИСТОРИЯ", "КАК ИСПРАВИТЬ",
            "ДЕФЕКТ", "ЗАГОЛОВОК ДЕФЕКТА")
_HEADER = re.compile(r"^[#*\s]*(" + "|".join(SECTIONS) + r")[*\s]*:[*\s]*(.*)$")
_DASH = re.compile(r"\s+[—–-]\s+")
_CODE_ITEM = re.compile(r"^(?:[-*]|\d+[.)])?\s*`?(?P<loc>[^\s`«]+?)`?\s*[—–:-]?\s*«(?P<quote>.+)»\s*$")
_LOG_QUOTE = re.compile(r"\[(?P<id>S\d+)\]\s*«(?P<quote>.+?)»(?P<rest>.*)$")
_STEP = re.compile(r"^\s*\d+[.)]\s+\S")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


@dataclass
class Result:
    revision: str = ""
    category: str = ""
    verdict: str = ""
    location: dict[str, Any] | None = None
    code: list[dict[str, Any]] = field(default_factory=list)
    log: dict[str, Any] | None = None
    history: str = ""
    commits: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    defect: str = ""
    defect_reason: str = ""
    defect_title: str = ""


def sections(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        match = _HEADER.match(raw)
        if match:
            current = match.group(1)
            found.setdefault(current, [])
            if match.group(2).strip():
                found[current].append(match.group(2).strip())
        elif current is not None and raw.strip():
            found[current].append(raw.strip())
    return found


def normalize(text: str) -> str:
    return " ".join(text.replace(" ", " ").split())


def quote_in(quote: str, text: str) -> bool:
    """Цитата (части через «…» — по порядку) есть в тексте без учёта пробелов."""
    haystack = normalize(text)
    position = 0
    for part in re.split(r"…|\.\.\.", quote):
        part = normalize(part)
        if not part:
            continue
        found = haystack.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return position > 0


def file_lines(tree: dict[str, Any], path: str) -> list[str] | None:
    target = Path(tree["tree"]) / path
    try:
        return target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def check_location(raw: str, state: dict[str, Any], errors: list[str],
                   where: str) -> dict[str, Any] | None:
    try:
        tree, path, line = sutlib.split_location(raw, state)
    except SutError as exc:
        errors.append(f"{where}: {exc}")
        return None
    lines = file_lines(tree, path)
    if lines is None:
        errors.append(f"{where}: файла {path} нет в ревизии {tree['repo']} {tree['sha'][:8]}")
        return None
    if line is None or not 1 <= line <= len(lines):
        errors.append(f"{where}: нужна строка от 1 до {len(lines)} — вид «{path}:<строка>»")
        return None
    return {"repo": tree["repo"], "path": path, "line": line, "sha": tree["sha"],
            "_lines": lines}


def validate(text: str, state: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[Result, list[str]]:
    found = sections(text)
    errors: list[str] = []
    result = Result()
    if not text.strip():
        return result, ["файл разбора пустой"]
    missing = [name for name in ("РЕВИЗИЯ", "ВЫВОД", "КАК ИСПРАВИТЬ", "ДЕФЕКТ")
               if not found.get(name)]
    if missing:
        errors.append(f"нет разделов: {', '.join(missing)} (названия — заглавными, как в формате)")

    revision = " ".join(found.get("РЕВИЗИЯ", []))
    result.revision = revision
    shas = [tree["sha"] for tree in state["trees"]]
    if revision and not any(sha.startswith(token) for token in _HEX.findall(revision.lower())
                            for sha in shas):
        errors.append("РЕВИЗИЯ: назови коммит выбранной ревизии — "
                      + ", ".join(f"{tree['repo']} {tree['sha'][:8]}" for tree in state["trees"]))

    verdict = " ".join(found.get("ВЫВОД", []))
    if verdict:
        head, phrase = (_DASH.split(verdict, maxsplit=1) + [""])[:2]
        category = head.strip().strip("*`«»").lower()
        if category not in sutlib.CATEGORIES:
            errors.append("ВЫВОД: первым словом — одна категория: " + ", ".join(sutlib.CATEGORIES))
        elif len(phrase.strip()) < 5:
            errors.append("ВЫВОД: после категории и «—» — причина одной фразой")
        result.category, result.verdict = category, phrase.strip()
    need_code = result.category not in ("", "неизвестно")

    place = found.get("МЕСТО", [])
    if place:
        loc_raw, what = (_DASH.split(place[0], maxsplit=1) + [""])[:2]
        location = check_location(loc_raw, state, errors, "МЕСТО")
        if location:
            location["what"] = what.strip()
            result.location = location
    elif need_code:
        errors.append("нет раздела МЕСТО: где в коде приложения причина (путь:строка — что там)")

    for item in found.get("КОД", []):
        match = _CODE_ITEM.match(item)
        if not match:
            errors.append(f"КОД: строка «{item[:80]}» — нужен вид «- путь:строка «дословная строка кода»»")
            continue
        location = check_location(match.group("loc"), state, errors, "КОД")
        if location is None:
            continue
        quote = match.group("quote").strip()
        lines = location["_lines"]
        line = location["line"]
        near = lines[max(0, line - 1 - QUOTE_RADIUS):line + QUOTE_RADIUS]
        if len(normalize(quote)) < 3 or not any(quote_in(quote, row) for row in near):
            errors.append(
                f"КОД: «{quote[:80]}» нет в {location['path']} рядом со строкой {line} — "
                "цитируй строку кода дословно (её можно сократить «…»)"
            )
            continue
        result.code.append({"repo": location["repo"], "path": location["path"], "line": line,
                            "quote": quote})
    if need_code and not result.code and not any("КОД:" in error for error in errors):
        errors.append("нет раздела КОД: хотя бы одна дословная строка кода с местом")

    link = " ".join(found.get("СВЯЗЬ С ПАДЕНИЕМ", []))
    if link:
        match = _LOG_QUOTE.search(link)
        by_id = {source["id"]: source for source in sources}
        if not match:
            errors.append("СВЯЗЬ С ПАДЕНИЕМ: нужен вид «[S<номер>] «цитата» — пояснение»")
        elif match.group("id") not in by_id:
            errors.append(f"СВЯЗЬ С ПАДЕНИЕМ: куска {match.group('id')} нет; есть: "
                          + ", ".join(by_id))
        elif not quote_in(match.group("quote"), str(by_id[match.group("id")].get("text", ""))):
            errors.append(f"СВЯЗЬ С ПАДЕНИЕМ: цитаты нет в куске {match.group('id')} — копируй дословно")
        else:
            result.log = {"source": match.group("id"), "quote": match.group("quote"),
                          "explanation": match.group("rest").strip(" —–-")}
    elif need_code:
        errors.append("нет раздела СВЯЗЬ С ПАДЕНИЕМ: цитата [S…] из данных падения и связь с кодом")

    history = " ".join(found.get("ИСТОРИЯ", []))
    result.history = history
    if history and not history.lower().startswith("нет данных"):
        trees = state["trees"]
        for token in _HEX.findall(history.lower()):
            known = any(sutlib.git(Path(tree["git_dir"]), "rev-parse", "--verify", "--quiet",
                                   f"{token}^{{commit}}", check=False).returncode == 0
                        for tree in trees)
            if not known:
                errors.append(f"ИСТОРИЯ: коммита {token} в репозитории нет — бери из ответа "
                              "history или compare")
            else:
                result.commits.append(token)

    result.steps = [step for step in found.get("КАК ИСПРАВИТЬ", []) if _STEP.match(step)]
    if found.get("КАК ИСПРАВИТЬ") and not result.steps:
        errors.append("КАК ИСПРАВИТЬ: шаги — нумерованным списком «1. …»")

    defect = " ".join(found.get("ДЕФЕКТ", []))
    if defect:
        lowered = defect.lower()
        decision = "не заводить" if lowered.startswith("не заводить") else \
            "заводить" if lowered.startswith("заводить") else ""
        reason = defect[len(decision):].strip(" —–-:")
        if not decision or len(reason) < 5:
            errors.append("ДЕФЕКТ: «заводить — почему» или «не заводить — почему»")
        result.defect, result.defect_reason = decision, reason
    title = " ".join(found.get("ЗАГОЛОВОК ДЕФЕКТА", []))
    if result.defect == "заводить" and not title:
        errors.append("ДЕФЕКТ «заводить» — нужен ЗАГОЛОВОК ДЕФЕКТА")
    if len(title) > TITLE_LIMIT:
        errors.append(f"ЗАГОЛОВОК ДЕФЕКТА длиннее {TITLE_LIMIT} символов")
    result.defect_title = title[:TITLE_LIMIT]
    if result.location:
        result.location.pop("_lines", None)
    return result, errors


def where(item: dict[str, Any], one: bool) -> str:
    return (f"{item['path']}:{item['line']}" if one
            else f"{item['repo']}:{item['path']}:{item['line']}")


def details(result: Result, one: bool, state: dict[str, Any]) -> list[str]:
    # Ревизию берём из выбора скрипта, а не со слов субагента: «как выбрана» (названа инженером
    # или приблизительно) пользователь должен видеть до подтверждения.
    rows = [f"Ревизия: {tree['repo']} {tree['ref']} ({tree['sha'][:8]}) — {tree['how']}"
            for tree in state["trees"]]
    if result.location:
        what = f" — {result.location['what']}" if result.location.get("what") else ""
        rows.append(f"Место: {where(result.location, one)}{what}")
    for item in result.code[:2]:
        rows.append(f"Код: {where(item, one)} «{item['quote']}»")
    if result.log:
        rows.append(f"Связь с падением: [{result.log['source']}] «{result.log['quote']}» — "
                    f"{result.log['explanation']}")
    if result.history and not result.history.lower().startswith("нет данных"):
        rows.append(f"История: {result.history}")
    if result.defect:
        title = f" Заголовок: {result.defect_title}" if result.defect_title else ""
        rows.append(f"Дефект: {result.defect} — {result.defect_reason}.{title}")
    return [row[:400] for row in rows[:8]]


def save(context: dict[str, Any], context_file: Path, state: dict[str, Any], result: Result,
         format_ok: bool, text: str) -> tuple[Path, list[str]]:
    problem = context.get("problem") or {}
    one = len(state["trees"]) == 1
    stem = context_file.stem
    out = context_file.parent
    cause = result.verdict or "причина в коде приложения не найдена"
    if result.location:
        cause += f" ({result.location['path'].rsplit('/', 1)[-1]}:{result.location['line']})"
    main = state["trees"][0]
    if result.location:
        main = sutlib.tree_of(state, result.location["repo"])
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    full = {
        "problem": problem.get("number"), "created": created, "format_ok": format_ok,
        "revisions": [{key: tree[key] for key in ("repo", "url", "ref", "sha", "how")}
                      for tree in state["trees"]],
        "verdict": {"category": result.category, "cause": result.verdict},
        "location": result.location, "code_quotes": result.code, "log_quote": result.log,
        "history": result.history, "commits": result.commits, "fix_steps": result.steps,
        "defect": {"needed": result.defect == "заводить", "reason": result.defect_reason,
                   "title": result.defect_title},
        "text": text,
    }
    (out / f"{stem}.result.json").write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
    rows = details(result, one, state)
    finding = {
        "extension": context.get("extension", "sut-code"),
        "problem": problem.get("number"),
        "category": result.category or "неизвестно",
        "title": (result.defect_title or cause)[:TITLE_LIMIT],
        "cause": cause,
        "fix": [re.sub(r"^\s*\d+[.)]\s*", "", step) for step in result.steps],
        "details": rows,
        "kb_fields": {key: value for key, value in {
            "repo": main["repo"], "revision": main["ref"], "commit": main["sha"][:12],
            "location": where(result.location, one) if result.location else "",
            "defect_title": result.defect_title,
        }.items() if value},
        "format_ok": format_ok,
        "created": created,
    }
    (out / f"{stem}.finding.json").write_text(json.dumps(finding, ensure_ascii=False, indent=2)
                                             + "\n", encoding="utf-8")
    report = out / f"{stem}.report.md"
    lines = [f"# Проблема {problem.get('number')} по коду приложения", "",
             f"**Вывод:** {result.category or 'неизвестно'} — {result.verdict}", ""]
    lines += [f"- {row}" for row in rows]
    if result.steps:
        lines += ["", "**Как исправить:**", *result.steps]
    if not format_ok:
        lines += ["", "_Формат нарушен: разбор не прошёл проверку, сохранить его нельзя._"]
    lines += ["", "## Разбор целиком", "", "```text", text.strip(), "```"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report, rows


def check(context: dict[str, Any], context_file: Path) -> list[str]:
    state = sutlib.load_state(context_file)
    problem = context.get("problem") or {}
    analysis = context_file.with_name(f"{context_file.stem}.txt")
    try:
        text = analysis.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SutError(f"нет файла разбора {analysis} — запиши его (write_file) по формату задания") \
            from exc
    result, errors = validate(text, state, problem.get("sources") or [])
    attempts_file = context_file.with_name(f"{context_file.stem}.check-attempts")
    if errors:
        try:
            attempts = int(attempts_file.read_text(encoding="utf-8")) + 1
        except (OSError, ValueError):
            attempts = 1
        attempts_file.write_text(str(attempts), encoding="utf-8")
        if attempts < MAX_ATTEMPTS:
            last = (" Следующая попытка — последняя: её примут с пометкой «формат нарушен»."
                    if attempts == MAX_ATTEMPTS - 1 else "")
            raise SutError("\n".join([
                f"Попытка {attempts} из {MAX_ATTEMPTS}: разбор {analysis} не прошёл проверку:",
                *(f"- {error}" for error in errors),
                f"Исправь файл (write_file) и снова выполни ту же команду check.{last}",
            ]))
    attempts_file.unlink(missing_ok=True)
    format_ok = not errors
    report, rows = save(context, context_file, state, result, format_ok, text)
    lines = [f"Проблема {problem.get('number')} по коду приложения — "
             f"{result.category or 'неизвестно'}: {result.verdict or 'причина в коде не найдена'}",
             *rows]
    if result.steps:
        lines.append("Как исправить: " + " ".join(result.steps))
    lines += [f"Внимание: {note}" for note in state.get("notes", [])]
    if any(tree["how"].startswith("приблизительно") for tree in state["trees"]):
        lines.append(f"Внимание: версия приложения выбрана приблизительно — если стенд был на "
                     f"другой, назови её: /alla:code {problem.get('number')} <ветка или тег>.")
    lines.append(f"Полный разбор: {report}")
    if not format_ok:
        lines.append("Разбор не прошёл проверку формата (" + "; ".join(errors[:3])
                     + ") — сохранить его в отчёт и базу знаний нельзя.")
    elif result.category in ("", "неизвестно"):
        lines.append("Причина в коде не найдена — сохранять в отчёт и базу знаний нечего.")
    else:
        lines.append("Подтверди вывод («да, верно») — сохраню его в отчёт и базу знаний; "
                     "«нет» — не сохраню.")
    return lines


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(check))
