"""Текущее состояние проблем для сценария разбора, отчёта и расширений.

Чтение не выполняет стадии next и не принимает файл обратной связи за подтверждение.
Изменение разбора известных проблем остаётся обязанностью sync_known_auto перед next.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import ClusterAnalysis
from alla_skill_lib.confirmed import load_confirmed
from alla_skill_lib.known_issues import (
    KnownIssues, current_auto_text, current_kb_matches, kb_auto_written, known_issues,
)
from alla_skill_lib.related import (
    CandidateGroup, Related, RelatedGroup, candidate_groups, parse_related,
    relate_data, related_groups, relation_scope, validate_related,
)
from alla_skill_lib.sources import check_entry_analysis


def normalized_steps(value: Any) -> str:
    """Одинаковое представление шагов разбора и подтверждённого вывода."""
    lines = value if isinstance(value, list) else str(value).splitlines()
    return "\n".join(re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip()
                     for line in lines if line.strip())


@dataclass(frozen=True)
class Diagnosis:
    category: str | None
    cause: str
    fix: str
    accepted: bool
    confirmed: bool
    # Файл feedback — вход remember, а не сам факт сохранения/подтверждения.
    feedback_unconfirmed: bool = False


@dataclass
class CurrentState:
    run: dict[str, Any]
    state: dict[str, Any]
    analyses: dict[str, ClusterAnalysis]
    errors: dict[str, list[str]]
    flagged: set[str]
    known: KnownIssues
    confirmed: dict[str, dict[str, Any]]
    current_matches: dict[str, list[dict[str, Any]]]
    candidates: list[CandidateGroup]
    related: Related
    diagnoses: dict[str, Diagnosis]

    @property
    def group_rows(self) -> list[dict[str, Any]]:
        return ([{"numbers": [int(n) for n in group.file_ids], "cause": group.cause}
                 for group in self.related.groups]
                + [{"numbers": [int(n) for n in group.file_ids],
                    "cause": group.record.description} for group in self.known.groups])


def current_state(
    paths: ws.RunPaths,
    *,
    run: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    analyses: dict[str, ClusterAnalysis] | None = None,
    flagged: set[str] | None = None,
    known_auto: set[str] | None = None,
) -> CurrentState:
    """Единый выбор диагнозов и действующих связей без изменения файлов разбора.

    next может передать уже проверенные analyses/flagged, чтобы не разбирать их дважды.
    Без них применяется та же check_entry_analysis; ошибки сохранены отдельно, чтобы
    next мог выдать обычное исправление формата, а не принять файл раньше срока.
    """
    run = ws.read_run(paths) if run is None else run
    if state is None:
        try:
            loaded = ws.read_json(paths.state_json)
        except (OSError, ValueError):
            loaded = {}
        state = loaded if isinstance(loaded, dict) else {}
    project = Path(run["project_root"])
    errors: dict[str, list[str]] = {}
    current_auto: set[str] = set()
    if analyses is None:
        analyses = {}
        for entry in run["clusters"]:
            file_id = entry["file_id"]
            path = paths.analysis(file_id)
            text = ws.read_text(path) if path.is_file() else ""
            if entry.get("kb_auto") and (not text.strip() or kb_auto_written(state, file_id, text)):
                text = current_auto_text(entry, paths, project) or ""
                if text:
                    current_auto.add(file_id)
            if text.strip():
                analyses[file_id], errors[file_id] = check_entry_analysis(
                    text, entry, project, paths)
    flagged = set(flagged) if flagged is not None else {
        file_id for file_id, items in errors.items() if items}
    if known_auto is None:
        # parse_analysis.raw убирает завершающий перевод строки: владельца файла
        # определяем по исходным байтам, а не по нормализованному тексту парсера.
        known_auto = current_auto | {
            file_id for file_id in analyses if file_id not in current_auto
            and paths.analysis(file_id).is_file()
            and kb_auto_written(state, file_id, ws.read_text(paths.analysis(file_id)))}
    known = known_issues(run, analyses, flagged, paths, known_auto)
    confirmed = load_confirmed(paths, run)
    matches = current_kb_matches(run, paths)
    candidates = candidate_groups(run, analyses, flagged, project, known, matches, set(confirmed))
    eligible = {entry["file_id"] for entry in run["clusters"]
                if entry["file_id"] in analyses and entry["file_id"] not in flagged
                and not entry.get("auto") and entry["file_id"] not in confirmed
                and entry["file_id"] not in known.refs and entry["file_id"] not in known.rejected
                and analyses[entry["file_id"]].category is not None
                and analyses[entry["file_id"]].consistency_kind != "different"}
    related = _accepted_related(paths, run, state, analyses, candidates, matches, eligible)
    diagnoses: dict[str, Diagnosis] = {}
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        sure = confirmed.get(file_id)
        pending_feedback = sure is None and paths.feedback(file_id).is_file()
        accepted = bool(analysis and analysis.observations
                        and file_id not in flagged and not analysis.sources_lost
                        and analysis.consistency_kind != "different" and not entry.get("auto")
                        and file_id not in state.get("skipped", []) and not pending_feedback)
        if sure is None and (file_id in known.rejected or file_id in known.mismatched):
            accepted = False
        diagnoses[file_id] = Diagnosis(
            category=sure["category"] if sure else analysis.category if analysis else None,
            cause=sure["cause"] if sure else analysis.cause_reason if analysis else "",
            fix=normalized_steps(sure["fix"] if sure else analysis.fix if analysis else ""),
            accepted=accepted, confirmed=sure is not None,
            feedback_unconfirmed=pending_feedback,
        )
    return CurrentState(run, state, analyses, errors, flagged, known, confirmed, matches,
                        candidates, related, diagnoses)


def _accepted_related(
    paths: ws.RunPaths,
    run: dict[str, Any],
    state: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    candidates: list[CandidateGroup],
    matches: dict[str, list[dict[str, Any]]],
    eligible: set[str],
) -> Related:
    # Отсутствие кандидатов снимает прежние связи даже до очередного next.
    if not candidates:
        return Related()
    if paths.related.is_file():
        digest = hashlib.sha256(relate_data(run, candidates, analyses, matches).encode()).hexdigest()
        if state.get("related_data_hash") == digest:
            decision = parse_related(ws.read_text(paths.related))
            # Текущий отказ/невалидное решение не восстанавливает прежние scopes.
            return (related_groups(decision, run) if not validate_related(decision, candidates)
                    else Related())
    sizes = {entry["file_id"]: int(entry["member_count"]) for entry in run["clusters"]}
    groups: list[RelatedGroup] = []
    for scoped in state.get("related_scopes", []):
        try:
            ids = [f"{int(n):02d}" for n in scoped["numbers"]]
            if (len(ids) < 2 or not set(ids) <= eligible
                    or scoped["fingerprint"] != relation_scope(analyses, ids, matches)
                    or not isinstance(scoped["cause"], str)):
                continue
            group = RelatedGroup(ids, scoped["cause"], sum(sizes[file_id] for file_id in ids))
        except (KeyError, TypeError, ValueError):
            continue
        if group not in groups:
            groups.append(group)
    return Related(groups)
