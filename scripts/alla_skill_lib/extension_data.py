"""Публичный снимок разбора и постоянный контекст расширений, без правил тикетов."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.proposals import parse_proposal
from alla_skill_lib.problem_state import current_state
from alla_skill_lib.rerun import RerunView, job_files, job_state
from alla_skill_lib.sources import describe, load_registry


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


def snapshot(paths: ws.RunPaths, settings: dict[str, Any]) -> dict[str, Any]:
    """Экспорт общего текущего состояния; стадии next не выполняются."""
    from alla_skill_lib.extensions import problem_tests

    current = current_state(paths)
    run = current.run
    analyses = current.analyses
    view = RerunView.of(job_state(job_files(paths)))
    problems: list[dict[str, Any]] = []
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        diagnosis = current.diagnoses[file_id]
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
        problems.append({
            "number": int(file_id), "label": entry.get("label", ""),
            "category": diagnosis.category,
            "cause": diagnosis.cause,
            "fix": diagnosis.fix,
            "what": analysis.what if analysis else "", "missing": analysis.missing if analysis else "",
            "valid": diagnosis.accepted and bool(observations), "confirmed": diagnosis.confirmed,
            "feedback_unconfirmed": diagnosis.feedback_unconfirmed,
            "tests": problem_tests(run, entry, limit=None), "sources": sources,
            "observations": observations, "has_fix": bool(proposal and proposal.is_fix),
            "rerun": view.summary_of(file_id) if view else "",
        })
    return {"schema": 1, "run_uid": ws.run_uid(paths), "launch_id": run.get("launch_id"),
            "project_root": str(run["project_root"]), "problems": problems, "groups": current.group_rows,
            "settings": settings}


def refresh_snapshot(context: dict[str, Any], paths: ws.RunPaths,
                     destination: Path | None = None) -> None:
    """Единственный канал: актуальные данные перед запуском процесса расширения."""
    data = snapshot(paths, context["settings"])
    destination = destination or (
        paths.extension_dir(context["extension"]) / f"snapshot-{digest(data)}.json")
    ws.write_json(destination, data)
    context["snapshot_path"] = str(destination)


def action_context(name: str, action: str, reports: Path, project: Path,
                   settings: dict[str, Any], *,
                   paths: ws.RunPaths | None = None) -> tuple[dict[str, Any], Path]:
    """Временный контекст действия; формат постоянных данных задаёт расширение."""
    directory = state_dir(reports, name, project)
    contexts = paths.extension_dir(name) if paths else directory / "contexts"
    context_path = contexts / f"action-{uuid.uuid4()}.json"
    context = {"api": 1, "event": "action", "action": action, "extension": name,
               "project_root": str(project.resolve()), "state_dir": str(directory),
               "out_dir": str(contexts), "reports_dir": str(reports.resolve()),
               "context_path": str(context_path), "settings": settings}
    if paths:
        run = ws.read_run(paths)
        context.update({"run_dir": str(paths.root), "launch_id": run.get("launch_id"),
                        "launch_name": run.get("launch_name"),
                        "launch_created": run.get("launch_created"),
                        "report_path": str(paths.report), "stem": context_path.stem,
                        "params": {}, "text_params": {}, "selected_numbers": []})
        decorate(context, paths, context_path)
    return context, context_path
