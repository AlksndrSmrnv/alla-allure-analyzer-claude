"""Известные проблемы: разборы, подтверждённые записью базы знаний, и их группы.

Одна причина часто даёт разные симптомы, и кластеризация разносит их по разным
проблемам. Если модель в нескольких разборах приняла одну запись базы знаний,
отчёт показывает их вместе — как одну известную проблему, ничего не теряя:
номера, разборы и правки остаются у своих проблем.

Ссылка разбора на запись (``БАЗА ЗНАНИЙ: <id>``) считается, только пока запись
подходит кластеру по текущим файлам ``alla-kb/`` — тем же правилом, что подбор
записей в ``prepare`` (``kb.record_matches``). Поэтому после ``reject`` проблема
выходит из группы при следующей сборке отчёта. Всё вычисляется заново при каждой
сборке и нигде не хранится.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import ClusterAnalysis
from alla_skill_lib.kb import (
    KB_TO_CATEGORY,
    KBRecord,
    ProjectKB,
    kb_label,
    normalize_fp,
    record_matches,
)


@dataclass(frozen=True)
class KnownRecord:
    """Запись базы знаний, на которую ссылается разбор (то, что нужно отчёту и сводке)."""

    kb_dir: str
    id: str
    title: str
    category: str | None  # категория скилла: тест | приложение | окружение | данные
    description: str
    first_step: str
    module: str = ""
    # False — папку базы знаний не найти: ссылка взята из разбора и снимка prepare как есть.
    verified: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.kb_dir, self.id)


@dataclass
class KnownIssue:
    """Одна известная проблема у нескольких проблем отчёта."""

    record: KnownRecord
    file_ids: list[str]
    size: int  # тестов во всех проблемах группы


@dataclass
class KnownIssues:
    """Подтверждённые ссылки разборов на записи и группы из них."""

    refs: dict[str, KnownRecord] = field(default_factory=dict)
    groups: list[KnownIssue] = field(default_factory=list)
    # Разбор ссылается на запись, но называет другую категорию причины.
    mismatched: dict[str, KnownRecord] = field(default_factory=dict)
    # Разбор опирается на запись, которую пользователь потом отверг для этой ошибки
    # (``reject``): текст разбора остался прежним, его причина не подтверждена.
    rejected: dict[str, KnownRecord] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # В прогоне записи из нескольких папок базы знаний (модулей): модуль надо называть.
    several_modules: bool = False

    def group_of(self, file_id: str) -> KnownIssue | None:
        return next((group for group in self.groups if file_id in group.file_ids), None)


def known_issues(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: ws.RunPaths,
) -> KnownIssues:
    """Подтвердить ссылки разборов на записи базы знаний и собрать группы.

    Проблема входит в группу записи, если разбор принят, ссылается на запись,
    запись подходит кластеру сейчас, категория разбора совпадает с категорией
    записи и примеры не названы «разными проблемами». Группа — от двух проблем.
    """
    result = KnownIssues()
    cache: dict[tuple[str, str], KBRecord | None] = {}
    unreachable: set[str] = set()
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        if analysis is None or file_id in flagged or not analysis.kb_ref:
            continue
        kb_dir = str(entry.get("kb_dir") or run.get("kb_dir") or "")
        if not kb_dir:
            continue
        directory = Path(kb_dir)
        if not directory.is_dir():
            # Разбор открыт не там, где готовился: проверить нечем — ссылка как в разборе.
            unreachable.add(kb_dir)
            snapshot = next((m for m in entry.get("kb", []) if m.get("id") == analysis.kb_ref), None)
            result.refs[file_id] = _from_snapshot(kb_dir, analysis.kb_ref, snapshot, entry)
            continue
        record = _load(cache, directory, analysis.kb_ref, run, result.notes)
        if record is None:
            continue
        if entry.get("signature") and entry["signature"] in record.rejected_signatures:
            result.rejected[file_id] = _from_record(kb_dir, record, entry)
            continue
        if not _matches(record, entry, paths):
            continue
        known = _from_record(kb_dir, record, entry)
        if analysis.category != known.category:
            result.mismatched[file_id] = known
        else:
            result.refs[file_id] = known

    project_root = Path(str(run.get("project_root") or "."))
    for kb_dir in sorted(unreachable):
        result.notes.append(
            f"База знаний {kb_label(project_root, Path(kb_dir))} недоступна — "
            "известные проблемы по ней не сгруппированы."
        )
    result.several_modules = len({ref.kb_dir for ref in (*result.refs.values(), *result.mismatched.values())}) > 1
    result.groups = _groups(run, analyses, result.refs)
    return result


def _groups(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    refs: dict[str, KnownRecord],
) -> list[KnownIssue]:
    sizes = {entry["file_id"]: int(entry["member_count"]) for entry in run["clusters"]}
    members: dict[tuple[str, str], list[str]] = {}
    for file_id, ref in refs.items():
        # Неоднородная группа (шаг 4) не объединяется: одна запись на смешанные тесты.
        if ref.verified and analyses[file_id].consistency_kind != "different":
            members.setdefault(ref.key, []).append(file_id)
    groups = [
        KnownIssue(
            record=refs[file_ids[0]],
            file_ids=sorted(file_ids, key=int),
            size=sum(sizes[file_id] for file_id in file_ids),
        )
        for file_ids in members.values()
        if len(file_ids) > 1
    ]
    return sorted(groups, key=lambda group: (-group.size, int(group.file_ids[0])))


def _load(
    cache: dict[tuple[str, str], KBRecord | None],
    directory: Path,
    entry_id: str,
    run: dict[str, Any],
    notes: list[str],
) -> KBRecord | None:
    key = (str(directory), entry_id)
    if key not in cache:
        kb = ProjectKB(directory, kb_label(Path(str(run.get("project_root") or ".")), directory))
        try:
            cache[key] = kb.get(entry_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cache[key] = None
            notes.append(f"База знаний: запись {kb.label}/{entry_id}.json не читается — {exc}")
    return cache[key]


def _matches(record: KBRecord, entry: dict[str, Any], paths: ws.RunPaths) -> bool:
    """Запись подходит кластеру сейчас: не отвергнута и узнаётся точно или по признаку."""
    evidence = paths.evidence(entry["file_id"])
    # Папка без доказательств (старая версия) — только точное совпадение сигнатуры.
    text = ws.read_text(evidence) if evidence.is_file() else ""
    return record_matches(record, entry.get("signature"), normalize_fp(text)) is not None


def _from_record(kb_dir: str, record: KBRecord, entry: dict[str, Any]) -> KnownRecord:
    return KnownRecord(
        kb_dir=kb_dir,
        id=record.id,
        title=record.title,
        category=KB_TO_CATEGORY.get(record.category),
        description=" ".join(record.description.split()),
        first_step=next((step.strip() for step in record.resolution_steps if step.strip()), ""),
        module=str(entry.get("module") or ""),
    )


def _from_snapshot(
    kb_dir: str,
    entry_id: str,
    snapshot: dict[str, Any] | None,
    entry: dict[str, Any],
) -> KnownRecord:
    snapshot = snapshot or {}
    steps = [str(step) for step in snapshot.get("steps") or []]
    return KnownRecord(
        kb_dir=kb_dir,
        id=entry_id,
        title=str(snapshot.get("title") or entry_id),
        category=snapshot.get("category"),
        description=" ".join(str(snapshot.get("description") or "").split()),
        first_step=next((step.strip() for step in steps if step.strip()), ""),
        module=str(entry.get("module") or ""),
        verified=False,
    )
