"""Публичный снимок разбора и постоянный контекст расширений, без правил тикетов."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import ClusterAnalysis, parse_analysis
from alla_skill_lib.confirmed import load_confirmed
from alla_skill_lib.known_issues import current_kb_matches, known_issues
from alla_skill_lib.proposals import parse_proposal
from alla_skill_lib.related import (
    candidate_groups, parse_related, relate_data, related_groups, relation_scope, validate_related,
)
from alla_skill_lib.rerun import RerunView, job_files, job_state
from alla_skill_lib.sources import check_entry_analysis, describe, load_registry


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def state_dir(reports_dir: Path, name: str, project_root: Path) -> Path:
    from alla_skill_lib.extensions import NAME_RE, ExtensionError

    if not NAME_RE.fullmatch(name):
        raise ExtensionError("Неверное имя расширения")
    project_key = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:16]
    return reports_dir.resolve() / ".extensions" / name / project_key


def decorate(context: dict[str, Any], paths: ws.RunPaths, context_file: Path) -> None:
    """Совместимые дополнительные поля; существующие поля событий не меняются."""
    context.update({
        "run_uid": ws.run_uid(paths),
        "context_path": str(context_file),
        "reports_dir": str(paths.reports_dir),
        "state_dir": str(state_dir(paths.reports_dir, context["extension"],
                                   Path(context["project_root"]))),
        "snapshot_command": [sys.executable, str(ws.ENTRYPOINT), "extensions", "snapshot",
                             context["extension"], "--context", str(context_file),
                             "--reports-dir", str(paths.reports_dir)],
    })


def text_files(raw: list[list[str]], directory: Path) -> dict[str, str]:
    """Читаем UTF-8 байты полностью, без universal-newline и strip."""
    from alla_skill_lib.extensions import PARAM_KEY_RE, ExtensionError

    values: dict[str, str] = {}
    for key, filename in raw:
        path = Path(filename).expanduser()
        resolved = path.resolve()
        if (not PARAM_KEY_RE.fullmatch(key) or key in values or path.is_symlink()
                or resolved.parent != directory.resolve() or resolved.suffix != ".txt"
                or not resolved.is_file()):
            raise ExtensionError("--text-file: уникальный ключ и обычный .txt внутри out_dir")
        try:
            data = resolved.read_bytes()
            value = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ExtensionError("--text-file: файл не читается как UTF-8") from exc
        if not value.strip() or len(value) > 16_000 or "\0" in value:
            raise ExtensionError("--text-file: непустой UTF-8 текст до 16000 символов, без NUL")
        values[key] = value
    return values


def _state(paths: ws.RunPaths) -> dict[str, Any]:
    try:
        data = ws.read_json(paths.state_json)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _steps(value: Any) -> str:
    lines = value if isinstance(value, list) else str(value).splitlines()
    return "\n".join(re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip()
                     for line in lines if line.strip())


def snapshot(paths: ws.RunPaths, settings: dict[str, Any]) -> dict[str, Any]:
    """Те же парсеры и проверка связи, что у next, без выполнения стадий next."""
    from alla_skill_lib.extensions import problem_tests

    run = ws.read_run(paths)
    project = Path(run["project_root"])
    state = _state(paths)
    analyses: dict[str, ClusterAnalysis] = {}
    flagged: set[str] = set()
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        if paths.analysis(file_id).is_file():
            checked, errors = check_entry_analysis(ws.read_text(paths.analysis(file_id)),
                                                     entry, project, paths)
            analyses[file_id] = checked
            if errors or checked.sources_lost or checked.consistency_kind == "different":
                flagged.add(file_id)
    known = known_issues(run, analyses, flagged, paths)
    confirmed = load_confirmed(paths, run)
    current_matches = current_kb_matches(run, paths)
    candidates = candidate_groups(run, analyses, flagged, project, known, current_matches,
                                  set(confirmed))
    groups: list[dict[str, Any]] = []
    use_scopes = True
    if candidates and paths.related.is_file():
        current_hash = hashlib.sha256(relate_data(run, candidates, analyses,
                                                  current_matches).encode()).hexdigest()
        decision = parse_related(ws.read_text(paths.related))
        if state.get("related_data_hash") == current_hash:
            # Текущий явный отказ от объединения сильнее прежнего отпечатка.
            # Невалидный новый файл также не восстанавливает старое решение молча.
            use_scopes = False
            if not validate_related(decision, candidates):
                groups = [{"numbers": [int(n) for n in group.file_ids], "cause": group.cause}
                          for group in related_groups(decision, run).groups]
    # Глобальная актуальность задания related не является актуальностью каждой
    # принятой группы: чужая правка не отменяет неизменившееся основание.
    eligible = {entry["file_id"] for entry in run["clusters"]
                if entry["file_id"] in analyses and entry["file_id"] not in flagged
                and not entry.get("auto") and entry["file_id"] not in confirmed
                and entry["file_id"] not in known.refs
                and entry["file_id"] not in known.rejected}
    for scoped in state.get("related_scopes", []) if use_scopes else []:
        ids = [f"{int(n):02d}" for n in scoped["numbers"]]
        if set(ids) <= eligible and scoped["fingerprint"] == relation_scope(
                analyses, ids, current_matches):
            row = {"numbers": scoped["numbers"], "cause": scoped["cause"]}
            if row not in groups:
                groups.append(row)
    groups += [{"numbers": [int(n) for n in group.file_ids],
                "cause": group.record.description}
               for group in known.groups]
    view = RerunView.of(job_state(job_files(paths)))
    problems: list[dict[str, Any]] = []
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        sure = confirmed.get(file_id)
        # Файл обратной связи сам по себе не означает выполненный remember.
        # Принимаем его диагноз лишь при точном действующем подтверждении в базе.
        pending_feedback = False
        if sure is None and paths.feedback(file_id).is_file():
            feedback = parse_analysis(ws.read_text(paths.feedback(file_id)))
            exact = [match for match in current_matches.get(file_id, [])
                     if match.get("origin") == "exact"
                     and match.get("category") == feedback.category
                     and match.get("description") == " ".join(feedback.cause_reason.split())
                     and _steps(match.get("steps", [])) == _steps(feedback.fix)]
            if len(exact) == 1:
                sure = {"category": exact[0]["category"], "cause": exact[0]["description"],
                        "fix": exact[0]["steps"]}
            else:
                pending_feedback = True
        registry = load_registry(paths.sources(file_id)) or {}
        sources = [{"id": source_id, "kind": record.get("kind"),
                    "title": describe(record), "text": record["text"],
                    "test_name": record.get("test_name")}
                   for source_id, record in registry.items() if record.get("kind") != "image"]
        observations = [{"source_id": item.source_id, "quote": item.quote}
                        for item in analysis.observations
                        if registry.get(item.source_id, {}).get("kind") != "image"] if analysis else []
        proposal = (parse_proposal(ws.read_text(paths.proposal(file_id)))
                    if paths.proposal(file_id).is_file() else None)
        valid = bool(analysis and not pending_feedback and file_id not in flagged and not entry.get("auto")
                     and file_id not in state.get("skipped", []) and observations)
        if not sure and (file_id in known.rejected or file_id in known.mismatched):
            valid = False
        problems.append({
            "number": int(file_id), "label": entry.get("label", ""),
            "category": sure["category"] if sure else analysis.category if analysis else None,
            "cause": sure["cause"] if sure else analysis.cause_reason if analysis else "",
            "fix": _steps(sure["fix"]) if sure else _steps(analysis.fix) if analysis else "",
            "what": analysis.what if analysis else "", "missing": analysis.missing if analysis else "",
            "valid": valid, "confirmed": bool(sure),
            "tests": problem_tests(run, entry, limit=None), "sources": sources,
            "observations": observations, "has_fix": bool(proposal and proposal.is_fix),
            "rerun": view.summary_of(file_id) if view else "",
        })
    return {"schema": 1, "run_uid": ws.run_uid(paths), "launch_id": run.get("launch_id"),
            "project_root": str(project), "problems": problems, "groups": groups,
            "settings": settings}


def export(name: str, context_path: str, *, project: Path | None = None,
           reports: Path | None = None) -> dict[str, str]:
    from alla_skill_lib.extensions import ExtensionError, find_active

    path = Path(context_path).expanduser().resolve()
    if (len(path.parents) < 3 or path.suffix != ".json" or path.parent.name != name
            or path.parent.parent.name != "extensions"):
        raise ExtensionError("Снимку нужен контекст расширения из папки разбора")
    paths = ws.RunPaths(path.parent.parent.parent)
    run = ws.read_run(paths)
    if ((project is not None and Path(run["project_root"]).resolve() != project.resolve())
            or (reports is not None and paths.reports_dir.resolve() != reports.resolve())):
        raise ExtensionError("Контекст снимка не принадлежит этому проекту или каталогу отчётов")
    context = ws.read_json(path)
    if context.get("extension") != name or context.get("run_uid") != ws.run_uid(paths):
        raise ExtensionError("Контекст относится к другому расширению или разбору")
    _, settings = find_active(Path(run["project_root"]), name)
    data = snapshot(paths, settings)
    sha = digest(data)
    destination = path.parent / f"snapshot-{sha}.json"
    ws.write_json(destination, data)
    return {"path": str(destination), "sha256": sha}


def reconcile(name: str, request_id: str, reports: Path, project: Path) -> tuple[str, str]:
    from alla_skill_lib.extensions import (
        ACTION_LIMIT_S, ExtensionError, find_active, run_script,
    )

    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except ValueError as exc:
        raise ExtensionError("request_id должен быть UUID") from exc
    extension, settings = find_active(project, name)
    script = extension.actions.get("reconcile")
    if script is None:
        raise ExtensionError(f"У расширения {name} нет обработчика reconcile")
    directory = state_dir(reports, name, project)
    record_path = directory / "requests" / f"{request_id}.json"
    if record_path.is_symlink():
        raise ExtensionError("Запись журнала не может быть ссылкой")
    try:
        record = ws.read_json(record_path)
    except (OSError, ValueError) as exc:
        raise ExtensionError("Запрос не найден в журнале этого проекта") from exc
    if (not isinstance(record, dict) or record.get("schema") != 1
            or record.get("extension") != name or record.get("request_id") != request_id
            or record.get("project_root") != str(project.resolve())):
        raise ExtensionError("Неверный конверт записи журнала")
    contexts = directory / "contexts"
    contexts.mkdir(parents=True, exist_ok=True)
    context_path = contexts / f"reconcile-{uuid.uuid4()}.json"
    ws.write_json(context_path, {
        "api": 1, "event": "reconcile", "extension": name,
        "project_root": str(project.resolve()), "state_dir": str(directory),
        "out_dir": str(contexts), "reports_dir": str(reports.resolve()),
        "request_id": request_id, "run_uid": record.get("run_uid"),
        "settings": settings, "request": record,
    })
    code, output = run_script(extension, script, context_path, project, timeout=ACTION_LIMIT_S)
    first, _, rest = output.partition("\n")
    if first not in ("STATUS: ok", "STATUS: error"):
        return "error", "Обработчик reconcile ответил без STATUS: ok|error"
    return ("ok" if code == 0 and first == "STATUS: ok" else "error"), rest.strip()
