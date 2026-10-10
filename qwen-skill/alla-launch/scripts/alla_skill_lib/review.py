"""Проверка законченного разбора для пилота: команда ``review``.

Запускается только по явной просьбе и после ``done`` — пока разбор идёт, модель о проверке
ничего не знает. Факты собираются из папки разбора (``run.json``, ``state.json``, разборы,
предложения) и журнала сеанса Qwen (``session_log``): ход разбора, правила исполнителя
(те же, что у стенда, ``session_rules``), сбои, признаки качества, время и токены. Оценку
диагнозов добавляет модель с чистым контекстом (``review_model``).

Результат — ``<папка разбора>/review/``: ``facts.json``, ``report.md`` (для пользователя,
с текстами из разбора) и ``pilot-summary.md`` (для разработчика: только числа и
фиксированные слова — без сообщений, логов, имён тестов, путей и кода).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.proposals import parse_proposal
from alla_skill_lib.session_log import SessionLog, load_session, skill_status, skill_subcommand
from alla_skill_lib.session_rules import RULES, RuleContext, skill_commands
from alla_skill_lib.sources import check_entry_analysis

REVIEW_DIRNAME = "review"
FACTS_SCHEMA = 1

PROCESS_CLEAN = "чисто"
PROCESS_NOTES = "с замечаниями"
PROCESS_FAILED = "сбой"
QUALITY_GOOD = "хорошо"
QUALITY_FAIR = "средне"
QUALITY_POOR = "плохо"
QUALITY_NONE = "не оценено"
# Пороги качества диагнозов по оценке проверяющей модели.
GOOD_CONFIRMED_SHARE = 0.8
GOOD_MAX_CATEGORY_DISAGREE = 0.1
FAIR_CONFIRMED_SHARE = 0.5

RULE_TITLES = {
    "shell_only_skill_commands": "из shell — только команды скилла",
    "no_code_search": "код проекта не искал сам (glob, grep)",
    "listed_code_only": "код открывал только из «Где искать код автотеста»",
    "allowed_reads": "не читал служебные файлы и файлы вне проекта",
    "allowed_writes": "писал только свои файлы разбора",
    "report_verbatim": "вывел краткий разбор дословно",
}
# Причины оценки хода: код (идёт в сводку для разработчика) → текст для человека.
REASONS = {
    "not_done": "разбор не дошёл до отчёта (STATUS: done)",
    "status_error": "команда скилла ответила STATUS: error",
    "traceback": "команда скилла упала без STATUS (traceback)",
    "missing_analyses": "есть кластеры без разбора",
    "rule": "нарушено правило исполнителя",
    "format_broken": "разбор принят с пометкой «формат нарушен»",
    "skill_problems": "модель сообщила о проблемах скилла",
    "fix_attempts": "разборы или предложения исправлялись после проверки",
    "batch_fallback": "пакетный режим выключился (субагенты не записали разборы)",
    "tool_errors": "ошибки инструментов (чтение несуществующего файла и т. п.)",
    "no_journal": "журнал сеанса Qwen не найден — правила не проверены",
}
FAILING_REASONS = {"not_done", "status_error", "traceback"}
# Причины, которые сами по себе не делают ход «с замечаниями»: пояснения к оценке.
INFO_REASONS = {"no_journal", "fix_attempts"}


# --- факты --------------------------------------------------------------------------------

def collect_facts(
    paths: ws.RunPaths, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Все факты о разборе. Оценки модели здесь нет — её добавляет ``review_model``."""
    run = ws.read_json(paths.run_json)
    state = _read_state(paths)
    project_root = Path(run["project_root"])
    log = load_session(paths.root, project_root, state, environ)
    clusters = _cluster_facts(paths, run, state, project_root)
    facts: dict[str, Any] = {
        "schema": FACTS_SCHEMA,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "skill_fingerprint": skill_fingerprint(),
        "run": {
            "launch_id": run.get("launch_id"),
            "launch_name": run.get("launch_name") or "",
            "folder": str(paths.root),
            "created_at": run.get("created_at", ""),
        },
        "size": {
            "tests_total": run.get("counts", {}).get("total", 0),
            "active_failures": run.get("counts", {}).get("active_failures", 0),
            "clusters": len(run["clusters"]),
            "manual": sum(1 for entry in run["clusters"] if not entry["auto"]),
            "auto": sum(1 for entry in run["clusters"] if entry["auto"]),
        },
        "process": _process_facts(paths, state, clusters),
        "journal": _journal_facts(log, project_root, paths),
        "clusters": clusters,
        "quality": _quality_facts(clusters),
        "assessment": None,
    }
    facts["grades"] = grades(facts)
    return facts


def _read_state(paths: ws.RunPaths) -> dict[str, Any]:
    try:
        loaded = ws.read_json(paths.state_json) if paths.state_json.is_file() else {}
    except (OSError, ValueError):
        loaded = {}
    return loaded if isinstance(loaded, dict) else {}


def _cluster_facts(
    paths: ws.RunPaths, run: Mapping[str, Any], state: Mapping[str, Any], project_root: Path
) -> list[dict[str, Any]]:
    attempts = state.get("attempts", {})
    skipped = set(state.get("skipped", []))
    result = []
    for number, entry in enumerate(run["clusters"], start=1):
        file_id = str(entry["file_id"])
        item: dict[str, Any] = {
            "number": number,
            "file_id": file_id,
            "tests": int(entry.get("member_count", 0)),
            "auto": bool(entry["auto"]),
            "skipped": file_id in skipped,
            "kb_matches": len(entry.get("kb") or []),
            "attempts": int((attempts.get(file_id) or {}).get("count", 0)),
            "proposal_attempts": int((attempts.get(f"proposal-{file_id}") or {}).get("count", 0)),
        }
        analysis_path = paths.analysis(file_id)
        text = ws.read_text(analysis_path) if analysis_path.is_file() else ""
        item["analysed"] = bool(text.strip())
        if item["analysed"]:
            analysis, errors = check_entry_analysis(text, entry, project_root, paths)
            log_quotes = sum(
                1 for obs in analysis.observations
                if str((analysis.sources or {}).get(obs.source_id, {}).get("kind")) not in ("message", "trace")
            )
            item.update({
                "title": analysis.title or analysis.what,
                "cause": analysis.cause,
                "category": analysis.category or "не указана",
                "observations": len(analysis.observations),
                "log_quotes": log_quotes,
                "unconfirmed_by_log": analysis.unconfirmed_by_log,
                "missing": bool(analysis.missing_text),
                "mixed": analysis.consistency_kind == "different",
                "kb_used": analysis.kb_ref is not None,
                "code": bool(analysis.code),
                "format_broken": bool(errors) and not item["auto"],
                "format_errors": len(errors) if not item["auto"] else 0,
            })
        proposal_path = paths.proposal(file_id)
        if proposal_path.is_file() and ws.read_text(proposal_path).strip():
            proposal = parse_proposal(ws.read_text(proposal_path))
            item["proposal"] = proposal.decision
            item["applied"] = paths.proposal_record(file_id).is_file()
        result.append(item)
    return result


def _process_facts(
    paths: ws.RunPaths, state: Mapping[str, Any], clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    manual = [item for item in clusters if not item["auto"]]
    batched = list(state.get("batched", []))
    return {
        "report_written": paths.report.is_file(),
        "summary_written": paths.summary.is_file() and bool(ws.read_text(paths.summary).strip()),
        "missing_analyses": [item["number"] for item in manual
                             if not item["analysed"] and not item["skipped"]],
        "skipped": [item["number"] for item in clusters if item["skipped"]],
        "fixed_analyses": sum(1 for item in manual if item["attempts"] > 0),
        "fix_attempts": sum(item["attempts"] + item["proposal_attempts"] for item in clusters),
        "format_broken": [item["number"] for item in manual if item.get("format_broken")],
        "batched": len(batched),
        "batch_fallback": bool(batched) and int(state.get("workers", 0) or 0) == 1,
        "proposals": sum(1 for item in clusters if item.get("proposal")),
        "proposal_fixes": sum(1 for item in clusters if item.get("proposal") == "fix"),
        "applied": sum(1 for item in clusters if item.get("applied")),
    }


def _journal_facts(log: SessionLog, project_root: Path, paths: ws.RunPaths) -> dict[str, Any]:
    if not log.calls:
        return {"found": False, "note": log.note}
    ctx = RuleContext(project_root, Path.home(), log.calls, log.final, [paths.root])
    rules = {name: check(ctx) for name, check in RULES.items()}
    if not log.reached_done:
        # Без done нечего сверять: отчёт агент и не должен был выводить.
        rules["report_verbatim"] = {"status": "n/a", "evidence": "разбор не дошёл до done"}
    commands = skill_commands(log.calls)
    status_errors = []
    tracebacks = []
    for call in commands:
        status = skill_status(call)
        if status == "error":
            lines = [line for line in call.result.splitlines()[1:] if line.strip()]
            status_errors.append({"command": skill_subcommand(call),
                                  "text": lines[0][:300] if lines else ""})
        elif status is None:
            tail = [line for line in call.result.splitlines() if line.strip()][-1:] or [""]
            tracebacks.append({"command": skill_subcommand(call), "text": tail[0][:300]})
    # Ответы команд скилла считаются по STATUS выше; здесь — прочие сбои инструментов.
    skill_ids = {id(call) for call in commands}
    tool_errors = Counter(call.name for call in log.calls if call.is_error and id(call) not in skill_ids)
    statuses = Counter(f"{skill_subcommand(call)}:{skill_status(call) or '—'}" for call in commands)
    return {
        "found": True,
        "note": log.note,
        "journals": len(log.journals),
        "model": log.model,
        "qwen_version": log.version,
        "reached_done": log.reached_done,
        "duration_seconds": log.duration_seconds,
        "usage": vars(log.usage).copy(),
        "api_errors": log.api_errors,
        "tool_calls": len(log.calls),
        "subagent_calls": sum(1 for call in log.calls if call.subagent),
        "subagents": log.subagents,
        "skill_commands": dict(sorted(statuses.items())),
        "status_errors": status_errors,
        "tracebacks": tracebacks,
        "tool_errors": dict(sorted(tool_errors.items())),
        "rules": rules,
        "skill_problems": log.skill_problems,
    }


def _quality_facts(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    analysed = [item for item in clusters if not item["auto"] and item["analysed"]]
    return {
        "analysed": len(analysed),
        "tests_analysed": sum(item["tests"] for item in analysed),
        "categories": dict(sorted(Counter(item["category"] for item in analysed).items())),
        "unknown": sum(1 for item in analysed if item["category"] == "неизвестно"),
        "unconfirmed_by_log": sum(1 for item in analysed if item["unconfirmed_by_log"]),
        "without_observations": sum(1 for item in analysed if item["observations"] == 0),
        "with_log_quotes": sum(1 for item in analysed if item["log_quotes"] > 0),
        "missing_named": sum(1 for item in analysed if item["missing"]),
        "mixed": sum(1 for item in analysed if item["mixed"]),
        "kb_used": sum(1 for item in analysed if item["kb_used"]),
        "with_code": sum(1 for item in analysed if item["code"]),
    }


def skill_fingerprint() -> str:
    """Отпечаток версии скилла (инструкции, справочники, код, зависимости): по нему
    разработчик узнаёт, на какой версии шёл пилот, — в копии скилла git нет."""
    root = Path(__file__).resolve().parents[2]
    files = [root / "SKILL.md", root / "requirements.txt"]
    for pattern in ("references/*.md", "agents/*.md", "scripts/**/*.py"):
        files += sorted(root.glob(pattern))
    digest = hashlib.sha256()
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


# --- оценки -------------------------------------------------------------------------------

def grades(facts: Mapping[str, Any]) -> dict[str, Any]:
    reasons = process_reasons(facts)
    if any(code in FAILING_REASONS for code, _ in reasons):
        process = PROCESS_FAILED
    elif any(code not in INFO_REASONS for code, _ in reasons):
        process = PROCESS_NOTES
    else:
        process = PROCESS_CLEAN
    quality, quality_note = quality_grade(facts.get("assessment"))
    return {"process": process, "process_reasons": [list(item) for item in reasons],
            "quality": quality, "quality_note": quality_note}


def process_reasons(facts: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(код причины, подробность). Код — из ``REASONS``; подробность — для человека."""
    process, journal = facts["process"], facts["journal"]
    reasons: list[tuple[str, str]] = []
    if not process["report_written"] or (journal["found"] and not journal["reached_done"]):
        reasons.append(("not_done", ""))
    if journal["found"]:
        for item in journal["status_errors"]:
            reasons.append(("status_error", f"{item['command']}: {item['text']}"))
        for item in journal["tracebacks"]:
            reasons.append(("traceback", f"{item['command']}: {item['text']}"))
    if process["missing_analyses"]:
        reasons.append(("missing_analyses", _numbers(process["missing_analyses"])))
    if journal["found"]:
        for name, result in journal["rules"].items():
            if result["status"] == "fail":
                reasons.append((f"rule:{name}", RULE_TITLES.get(name, name)))
    else:
        reasons.append(("no_journal", str(journal.get("note", ""))))
    if process["format_broken"]:
        reasons.append(("format_broken", _numbers(process["format_broken"])))
    if journal["found"] and journal["skill_problems"]:
        reasons.append(("skill_problems", str(len(journal["skill_problems"]))))
    if process["batch_fallback"]:
        reasons.append(("batch_fallback", ""))
    if journal["found"] and journal["tool_errors"]:
        reasons.append(("tool_errors", ", ".join(f"{k} ×{v}" for k, v in journal["tool_errors"].items())))
    if process["fix_attempts"]:
        reasons.append(("fix_attempts", str(process["fix_attempts"])))
    return reasons


def reason_text(code: str) -> str:
    if code.startswith("rule:"):
        return f"{REASONS['rule']}: {RULE_TITLES.get(code[5:], code[5:])}"
    return REASONS.get(code, code)


def quality_grade(assessment: Mapping[str, Any] | None) -> tuple[str, str]:
    """Оценка качества диагнозов по ответу проверяющей модели."""
    if not assessment or not assessment.get("problems"):
        reason = (assessment or {}).get("error") or "оценка модели не получена"
        return QUALITY_NONE, str(reason)
    problems = assessment["problems"]
    total = len(problems)
    confirmed = sum(1 for p in problems if p.get("cause") == "подтверждена")
    disagree = sum(1 for p in problems if p.get("category") == "не согласен")
    share, disagree_share = confirmed / total, disagree / total
    note = f"причина подтверждена у {confirmed} из {total}, несогласий по категории — {disagree}"
    if share >= GOOD_CONFIRMED_SHARE and disagree_share <= GOOD_MAX_CATEGORY_DISAGREE:
        return QUALITY_GOOD, note
    if share >= FAIR_CONFIRMED_SHARE:
        return QUALITY_FAIR, note
    return QUALITY_POOR, note


def _numbers(values: list[int]) -> str:
    return ", ".join(str(value) for value in values)


# --- запись -------------------------------------------------------------------------------

def review_dir(paths: ws.RunPaths) -> Path:
    return paths.root / REVIEW_DIRNAME


def write_review(paths: ws.RunPaths, facts: dict[str, Any]) -> tuple[Path, Path]:
    """Записать ``facts.json``, ``report.md`` и ``pilot-summary.md``; вернуть пути отчётов."""
    folder = review_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    ws.write_json(folder / "facts.json", facts)
    report, summary = folder / "report.md", folder / "pilot-summary.md"
    ws.write_text(report, render_report(facts))
    ws.write_text(summary, render_pilot_summary(facts))
    return report, summary


# --- отчёт для пользователя ---------------------------------------------------------------

def _grade_lines(facts: Mapping[str, Any]) -> list[str]:
    g = facts["grades"]
    lines = [f"Ход разбора: **{g['process']}**"]
    for code, detail in g["process_reasons"]:
        lines.append(f"- {reason_text(code)}" + (f" — {detail}" if detail else ""))
    lines.append(f"Качество диагнозов: **{g['quality']}** — {g['quality_note']}")
    return lines


def render_report(facts: Mapping[str, Any]) -> str:
    run, size, process, journal = facts["run"], facts["size"], facts["process"], facts["journal"]
    name = f" «{run['launch_name']}»" if run["launch_name"] else ""
    out = [f"# Проверка разбора прогона #{run['launch_id']}{name}", "",
           f"Папка разбора: {run['folder']}  ", f"Проверено: {facts['created_at']}  ",
           f"Версия скилла: {facts['skill_fingerprint']}", "", "## Итог", "", *_grade_lines(facts), ""]

    out += ["## Ход разбора", "",
            f"- Тестов {size['tests_total']}, активных падений {size['active_failures']}, "
            f"проблем {size['clusters']} (разбирала модель — {size['manual']}, скрипт — {size['auto']}).",
            f"- Отчёт: {'есть' if process['report_written'] else 'нет'}; общая сводка: "
            f"{'есть' if process['summary_written'] else 'нет'}.",
            f"- Разборов исправляли после проверки: {process['fixed_analyses']} "
            f"(всего попыток исправления: {process['fix_attempts']}).",
            f"- «Формат нарушен»: {_numbers(process['format_broken']) or 'нет'}; без разбора: "
            f"{_numbers(process['missing_analyses']) or 'нет'}; пропущено по просьбе: "
            f"{_numbers(process['skipped']) or 'нет'}.",
            f"- Пакетами субагентам выдано кластеров: {process['batched']}"
            + ("; пакетный режим выключился" if process["batch_fallback"] else "") + ".",
            f"- Предложений правок: {process['proposals']} (правок кода — {process['proposal_fixes']}, "
            f"применено — {process['applied']}).", ""]

    out += ["## Правила исполнителя", ""]
    if not journal["found"]:
        out += [f"Не проверены: {journal['note']}.", ""]
    else:
        out += ["| Правило | Итог | Подробности |", "|---|---|---|"]
        for rule, result in journal["rules"].items():
            mark = {"pass": "соблюдено", "fail": "**нарушено**"}.get(result["status"], "не применимо")
            out.append(f"| {RULE_TITLES.get(rule, rule)} | {mark} | {_cell(result['evidence'])} |")
        out.append("")

        out += ["## Сбои и проблемы скилла", ""]
        items = [f"- `{e['command']}` → STATUS: error: {e['text']}" for e in journal["status_errors"]]
        items += [f"- `{e['command']}` упала без STATUS: {e['text']}" for e in journal["tracebacks"]]
        items += [f"- ошибки инструмента {k}: {v}" for k, v in journal["tool_errors"].items()]
        items += [f"- модель сообщила: {text}" for text in journal["skill_problems"]]
        out += (items or ["Не было."]) + [""]

    out += ["## Разборы по проблемам", "",
            "| № | Тестов | Категория | Цитат (из лога) | Замечания |", "|---|---|---|---|---|"]
    for item in facts["clusters"]:
        notes = _cluster_notes(item)
        if item["auto"]:
            out.append(f"| {item['number']} | {item['tests']} | — | — | разобрал скрипт |")
        elif not item["analysed"]:
            out.append(f"| {item['number']} | {item['tests']} | — | — | нет разбора |")
        else:
            out.append(f"| {item['number']} | {item['tests']} | {item['category']} | "
                       f"{item['observations']} ({item['log_quotes']}) | {notes or '—'} |")
    out.append("")

    assessment = facts.get("assessment")
    out += ["## Оценка диагнозов проверяющей моделью", ""]
    if assessment and assessment.get("problems"):
        out.append(f"Оценено проблем: {len(assessment['problems'])} из {facts['quality']['analysed']} "
                   f"(они покрывают {assessment.get('tests_covered', 0)} упавших тестов из "
                   f"{facts['quality']['tests_analysed']}).")
        out += ["", "| № | Причина | Категория | Действия | Замечание |", "|---|---|---|---|---|"]
        for p in assessment["problems"]:
            category = p.get("category", "")
            if category == "не согласен" and p.get("category_expected"):
                category += f" (скорее «{p['category_expected']}»)"
            out.append(f"| {p.get('number')} | {p.get('cause', '')} | {category} | "
                       f"{p.get('actions', '')} | {_cell(p.get('note', ''))} |")
        if assessment.get("summary"):
            out += ["", f"Итог проверяющего: {assessment['summary']}"]
    else:
        out.append(f"Нет: {facts['grades']['quality_note']}.")
    out.append("")

    if journal["found"]:
        usage = journal["usage"]
        out += ["## Затраты", "",
                f"- Время разбора: {_duration(journal['duration_seconds'])}; модель "
                f"{journal['model'] or '?'}, Qwen Code {journal['qwen_version'] or '?'}.",
                f"- Запросов к модели: {usage['requests']}, токенов: {usage['total']} "
                f"(из кэша {usage['cached']}, ответ {usage['output']}); ошибок API: {journal['api_errors']}.",
                f"- Вызовов инструментов: {journal['tool_calls']} (у субагентов — "
                f"{journal['subagent_calls']}), субагентов: {journal['subagents']}.", ""]
    out += ["## Что передать разработчику", "",
            "Файл `pilot-summary.md` рядом с этим отчётом: в нём только числа и названия проверок, "
            "без сообщений, логов, имён тестов и кода.", ""]
    return "\n".join(out)


def _cluster_notes(item: Mapping[str, Any]) -> str:
    notes = []
    if item.get("format_broken"):
        notes.append("формат нарушен")
    if item.get("attempts"):
        notes.append(f"исправлялся {item['attempts']} раз")
    if item.get("unconfirmed_by_log"):
        notes.append("причина не подтверждена логом")
    if item.get("observations") == 0:
        notes.append("нет цитат")
    if item.get("mixed"):
        notes.append("разные проблемы в группе")
    if item.get("kb_used"):
        notes.append("опирается на базу знаний")
    return ", ".join(notes)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")[:200]


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    return f"{seconds // 60} мин {seconds % 60} с" if seconds >= 60 else f"{seconds} с"


# --- сводка для разработчика (без данных прогона) -----------------------------------------

def render_pilot_summary(facts: Mapping[str, Any]) -> str:
    """Только числа, коды и фиксированные слова скилла. Ни одной строки из прогона, задания,
    разбора или журнала: это охраняет ``test_skill_review``."""
    size, process, journal, quality = facts["size"], facts["process"], facts["journal"], facts["quality"]
    g = facts["grades"]
    out = ["# alla · сводка проверки разбора", "",
           "Только числа и названия проверок: без сообщений, логов, имён тестов, путей и кода.", "",
           f"- версия скилла: {facts['skill_fingerprint']}",
           f"- дата: {str(facts['created_at'])[:10]}",
           f"- ход разбора: {g['process']}",
           f"- качество диагнозов: {g['quality']}", "",
           "## Причины оценки хода", ""]
    codes = Counter(code for code, _ in g["process_reasons"])
    out += [f"- {code} ×{count}" for code, count in sorted(codes.items())] or ["- нет"]
    out += ["", "## Размер", "",
            f"- тестов: {size['tests_total']}, активных падений: {size['active_failures']}",
            f"- проблем: {size['clusters']} (модель: {size['manual']}, скрипт: {size['auto']})", "",
            "## Ход", "",
            f"- отчёт: {_yes(process['report_written'])}, сводка: {_yes(process['summary_written'])}",
            f"- без разбора: {len(process['missing_analyses'])}, пропущено: {len(process['skipped'])}",
            f"- исправлялись: {process['fixed_analyses']}, попыток исправления: {process['fix_attempts']}",
            f"- формат нарушен: {len(process['format_broken'])}",
            f"- в пакетах: {process['batched']}, пакетный режим выключился: {_yes(process['batch_fallback'])}",
            f"- предложений: {process['proposals']} (правок: {process['proposal_fixes']}, "
            f"применено: {process['applied']})", "",
            "## Журнал сеанса", ""]
    if not journal["found"]:
        out.append("- не найден")
    else:
        usage = journal["usage"]
        out += [f"- модель: {_safe_word(journal['model'])}, Qwen Code: {_safe_word(journal['qwen_version'])}",
                f"- время: {journal['duration_seconds']} с, запросов: {usage['requests']}, "
                f"токенов: {usage['total']} (кэш {usage['cached']}, ответ {usage['output']}), "
                f"ошибок API: {journal['api_errors']}",
                f"- вызовов инструментов: {journal['tool_calls']} (субагенты: {journal['subagent_calls']}), "
                f"субагентов: {journal['subagents']}",
                "- команды скилла: " + (", ".join(f"{k} ×{v}" for k, v in journal["skill_commands"].items()) or "нет"),
                f"- STATUS: error: {len(journal['status_errors'])} "
                + _commands(journal["status_errors"]),
                f"- без STATUS (traceback): {len(journal['tracebacks'])} " + _commands(journal["tracebacks"]),
                "- ошибки инструментов: " + (", ".join(f"{k} ×{v}" for k, v in journal["tool_errors"].items()) or "нет"),
                f"- проблем скилла сообщено: {len(journal['skill_problems'])}", "",
                "## Правила исполнителя", ""]
        out += [f"- {rule}: {result['status']}" for rule, result in journal["rules"].items()]
    out += ["", "## Признаки качества разборов", "",
            f"- разобрано моделью: {quality['analysed']} (тестов: {quality['tests_analysed']})",
            "- категории: " + (", ".join(f"{k} ×{v}" for k, v in quality["categories"].items()) or "нет"),
            f"- неизвестно: {quality['unknown']}, причина не подтверждена логом: {quality['unconfirmed_by_log']}",
            f"- без цитат: {quality['without_observations']}, с цитатами из лога: {quality['with_log_quotes']}",
            f"- назван недостающий источник: {quality['missing_named']}, разные проблемы в группе: {quality['mixed']}",
            f"- с базой знаний: {quality['kb_used']}, с местом в коде: {quality['with_code']}", "",
            "## Оценка проверяющей модели", ""]
    assessment = facts.get("assessment")
    if assessment and assessment.get("problems"):
        problems = assessment["problems"]
        for field_name, title in (("cause", "причина"), ("category", "категория"), ("actions", "действия")):
            counts = Counter(str(p.get(field_name, "")) for p in problems)
            out.append(f"- {title}: " + ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items())))
        out.append(f"- оценено: {len(problems)} из {quality['analysed']}, тестов покрыто: "
                   f"{assessment.get('tests_covered', 0)}")
        expected = Counter(str(p.get("category_expected")) for p in problems
                           if p.get("category") == "не согласен" and p.get("category_expected"))
        if expected:
            out.append("- категория по мнению проверяющего: "
                       + ", ".join(f"{k} ×{v}" for k, v in sorted(expected.items())))
        usage = assessment.get("usage") or {}
        if usage:
            out.append(f"- токенов проверки: {usage.get('total', 0)}, время: {assessment.get('seconds', 0)} с")
    else:
        out.append(f"- нет ({_safe_reason(assessment)})")
    return "\n".join(out) + "\n"


def _yes(value: bool) -> str:
    return "да" if value else "нет"


def _commands(items: list[Mapping[str, Any]]) -> str:
    counts = Counter(str(item["command"]) for item in items)
    return f"({', '.join(f'{k} ×{v}' for k, v in sorted(counts.items()))})" if counts else ""


_SAFE_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./-_:")


def _safe_word(value: str) -> str:
    """Имя модели или версия: только из безопасных символов, иначе «?»."""
    return value if value and set(value) <= _SAFE_WORD_CHARS and len(value) <= 80 else "?"


def _safe_reason(assessment: Mapping[str, Any] | None) -> str:
    """Причина без оценки — только код, без текста ошибки (он может содержать данные)."""
    if not assessment:
        return "not_run"
    return str(assessment.get("error_code") or "no_problems")


# --- краткий итог в терминал --------------------------------------------------------------

def render_console(facts: Mapping[str, Any], report: Path, summary: Path) -> str:
    g, journal, quality = facts["grades"], facts["journal"], facts["quality"]
    run = facts["run"]
    lines = [f"Проверка разбора прогона #{run['launch_id']}", "",
             f"Ход разбора: {g['process']}"]
    for code, detail in g["process_reasons"]:
        lines.append(f"  - {reason_text(code)}" + (f" — {detail}" if detail else ""))
    lines.append(f"Качество диагнозов: {g['quality']} — {g['quality_note']}")
    if journal["found"]:
        rules = journal["rules"].values()
        kept = sum(1 for result in rules if result["status"] == "pass")
        checked = sum(1 for result in rules if result["status"] in ("pass", "fail"))
        usage = journal["usage"]
        lines.append(f"Правила исполнителя: соблюдено {kept} из {checked}; сбоев команд: "
                     f"{len(journal['status_errors']) + len(journal['tracebacks'])}; проблем скилла: "
                     f"{len(journal['skill_problems'])}.")
        lines.append(f"Время: {_duration(journal['duration_seconds'])}, токенов: {usage['total']}.")
    lines.append(f"Разобрано моделью: {quality['analysed']}; «неизвестно»: {quality['unknown']}; "
                 f"причина не подтверждена логом: {quality['unconfirmed_by_log']}.")
    lines += ["", f"Полный отчёт: {report}",
              f"Сводка для разработчика (без данных прогона): {summary}"]
    return "\n".join(lines)


def assess(paths: ws.RunPaths, facts: Mapping[str, Any]) -> dict[str, Any]:
    """Оценка диагнозов моделью с чистым контекстом (отдельный процесс ``qwen``)."""
    from alla_skill_lib.review_model import assess_diagnoses

    return assess_diagnoses(paths, facts)
