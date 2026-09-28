"""Обратная связь пользователя → база знаний проекта: ``remember`` и ``reject``.

Пользователь говорит в чате причину проблемы и рецепт (или подтверждает
разбор). Агент записывает это в ``feedback/NN.md`` и вызывает ``remember``:
скрипт проверяет текст, признак ошибки и сохраняет запись в ``alla-kb/``.
Сигнатура кластера становится подтверждённой — в следующий раз та же ошибка
узнаётся точно. ``reject`` запоминает, что запись к этой ошибке не относится.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.kb import (
    CATEGORY_TO_KB,
    MAX_FINGERPRINT_LINES,
    KBRecord,
    ProjectKB,
    find_kb_dir,
    fingerprint_lines,
    make_entry_id,
    missing_fingerprint_lines,
    secret_lines,
)

MAX_TITLE_CHARS = 120
FEEDBACK_FORMAT = """\
НАЗВАНИЕ: <коротко, 3–8 слов>
ПРИЧИНА: <тест|приложение|окружение|данные> — <причина со слов пользователя>
КАК ИСПРАВИТЬ:
1. <шаг рецепта>
ПРИЗНАК: <1–3 строки дословно из сообщения об ошибке или лога>   (необязательно)"""


def find_entry(run: dict[str, Any], cluster: str) -> dict[str, Any] | None:
    """Кластер по номеру из отчёта: «3», «03», «003»."""
    try:
        number = int(cluster)
    except ValueError:
        return None
    return next((e for e in run["clusters"] if int(e["file_id"]) == number), None)


def project_kb(run: dict[str, Any]) -> ProjectKB:
    return ProjectKB(Path(run.get("kb_dir") or find_kb_dir(Path(run["project_root"]))))


def remember(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    entry_id: str | None,
    today: date,
) -> tuple[str, str]:
    """Сохранить причину и рецепт для кластера. Возвращает (статус, текст)."""
    file_id = entry["file_id"]
    blocked = _blocked(paths, entry)
    if blocked:
        return "error", blocked

    feedback_path = paths.feedback(file_id)
    from_feedback = feedback_path.is_file() and bool(feedback_path.read_text(encoding="utf-8").strip())
    source = feedback_path if from_feedback else paths.analysis(file_id)
    parsed = parse_analysis(source.read_text(encoding="utf-8"))
    project_root = Path(run["project_root"])
    if not from_feedback:
        offered = frozenset(match["id"] for match in entry.get("kb", []))
        if validate_analysis(parsed, project_root, offered):
            return "fix", _fix_body(paths, file_id, [
                "разбор кластера не прошёл проверку формата — запиши причину и рецепт "
                "в файл обратной связи"
            ])

    kb = project_kb(run)
    record: KBRecord | None = None
    if entry_id:
        try:
            record = kb.get(entry_id)
        except ValueError as exc:
            return "error", str(exc)
        if record is None:
            return "error", f"В {kb.directory} нет записи «{entry_id}»."

    errors: list[str] = []
    if parsed.category is None or parsed.category == "неизвестно":
        errors.append(
            "в «ПРИЧИНА:» первым словом нужна категория тест / приложение / окружение / "
            "данные («неизвестно» запоминать нельзя)"
        )
    steps = [_step_text(line) for line in parsed.fix.splitlines() if _step_text(line)]
    if not steps:
        errors.append("нет «КАК ИСПРАВИТЬ:» — рецепт обязателен")
    if record is None or parsed.fingerprint:
        # Признак проверяется, только когда он сохраняется: у обновляемой
        # записи без нового ПРИЗНАКА эту ошибку узнает подтверждённая сигнатура.
        fingerprint = parsed.fingerprint or str(entry.get("fingerprint") or "")
        errors.extend(_fingerprint_errors(fingerprint, paths.evidence(file_id)))
    else:
        fingerprint = record.error_example
    if errors:
        return "fix", _fix_body(paths, file_id, errors)

    assert parsed.category is not None
    signature = str(entry["signature"])
    records, _warnings = kb.load()
    title = _title(parsed.title or parsed.what or parsed.cause_reason)
    if record is None:
        owner = next((r for r in records if signature in r.confirmed_signatures), None)
        if owner is not None:
            return "error", (
                f"Эту ошибку уже подтверждали в записи «{owner.id}» ({owner.title}). "
                f"Чтобы обновить её: {ws.skill_command('remember', file_id, '--entry', owner.id, '--run', str(paths.root))}"
            )
        new_id = make_entry_id(title, fingerprint)
        if kb.path_for(new_id).exists():
            return "error", (
                f"Запись «{new_id}» уже есть. Чтобы обновить её: "
                f"{ws.skill_command('remember', file_id, '--entry', new_id, '--run', str(paths.root))}"
            )
        record = KBRecord(
            id=new_id,
            title=title,
            category=CATEGORY_TO_KB[parsed.category],
            description=" ".join(parsed.cause_reason.split()),
            resolution_steps=steps,
            error_example="\n".join(fingerprint_lines(fingerprint)),
            created={"date": today.isoformat(), "launch_id": run["launch_id"], "cluster": file_id},
        )
        action = "новая запись"
    else:
        if parsed.title:
            record.title = title
        record.category = CATEGORY_TO_KB[parsed.category]
        record.description = " ".join(parsed.cause_reason.split())
        record.resolution_steps = steps
        if parsed.fingerprint:
            record.error_example = "\n".join(fingerprint_lines(parsed.fingerprint))
        action = "запись обновлена"

    record.confirm(signature)
    changed = [kb.save(record)]
    for other in records:
        if other.id != record.id and signature in other.confirmed_signatures:
            other.confirmed_signatures.remove(signature)
            changed.append(kb.save(other))
    return "saved", "\n".join([
        f"Запомнено в базе знаний ({action}): {record.id} — «{record.title}»",
        "Изменены файлы:",
        *(f"- {path}" for path in changed),
        "Скажи пользователю, что рецепт сохранён, и напомни закоммитить папку "
        f"{kb.directory.name}/, чтобы им пользовалась вся команда.",
    ])


def reject(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    entry_id: str,
) -> tuple[str, str]:
    """Запомнить, что запись базы знаний к ошибке этого кластера не относится."""
    blocked = _blocked(paths, entry)
    if blocked:
        return "error", blocked
    kb = project_kb(run)
    try:
        record = kb.get(entry_id)
    except ValueError as exc:
        return "error", str(exc)
    if record is None:
        return "error", f"В {kb.directory} нет записи «{entry_id}»."
    record.reject(str(entry["signature"]))
    path = kb.save(record)
    return "saved", "\n".join([
        f"Запись {record.id} больше не будет предлагаться для этой ошибки.",
        f"Изменён файл: {path}",
        f"Напомни пользователю закоммитить папку {kb.directory.name}/.",
    ])


def _blocked(paths: ws.RunPaths, entry: dict[str, Any]) -> str | None:
    if entry.get("auto") or not entry.get("signature"):
        return "У этого кластера нет данных об ошибке (сообщения, трейса, лога) — запоминать нечего."
    if not paths.evidence(entry["file_id"]).is_file():
        return "Разбор создан старой версией скилла — выполни prepare заново, затем повтори."
    return None


def _fingerprint_errors(fingerprint: str, evidence_path: Path) -> list[str]:
    lines = fingerprint_lines(fingerprint)
    if not lines:
        return ["нет признака ошибки — добавь «ПРИЗНАК:» (1–3 строки из текста ошибки или лога)"]
    errors: list[str] = []
    if len(lines) > MAX_FINGERPRINT_LINES:
        errors.append(f"в «ПРИЗНАК:» больше {MAX_FINGERPRINT_LINES} строк — оставь самые характерные")
    for line in secret_lines(fingerprint):
        errors.append(f"строка признака похожа на секрет, её нельзя коммитить: «{line[:60]}»")
    evidence = evidence_path.read_text(encoding="utf-8")
    for line in missing_fingerprint_lines(fingerprint, evidence):
        errors.append(
            f"строки признака нет в данных кластера: «{line[:120]}» — скопируй её дословно "
            "из сообщения об ошибке, трейса или лога в задании кластера"
        )
    return errors


def _fix_body(paths: ws.RunPaths, file_id: str, errors: list[str]) -> str:
    return "\n".join([
        "Обратная связь не сохранена:",
        *(f"- {error}" for error in errors),
        f"Запиши или исправь файл: {paths.feedback(file_id)}",
        "Формат:",
        FEEDBACK_FORMAT,
        f"Затем выполни: {ws.skill_command('remember', file_id, '--run', str(paths.root))}",
    ])


def _title(text: str) -> str:
    first = " ".join(text.split()).split(". ")[0].rstrip(".")
    return first if len(first) <= MAX_TITLE_CHARS else first[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def _step_text(line: str) -> str:
    stripped = line.strip()
    for prefix_end in (". ", ") "):
        head, sep, tail = stripped.partition(prefix_end)
        if sep and head.isdigit():
            return tail.strip()
    return stripped.lstrip("-*• ").strip()
