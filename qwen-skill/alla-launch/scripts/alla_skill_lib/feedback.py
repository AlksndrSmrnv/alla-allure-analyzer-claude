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
from alla_skill_lib.agent_rules import FEEDBACK_FORMAT_REF, reference_line
from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.kb import (
    CATEGORY_TO_KB,
    MAX_FINGERPRINT_LINES,
    KBRecord,
    ProjectKB,
    find_kb_dir,
    fingerprint_lines,
    kb_label,
    make_entry_id,
    missing_fingerprint_lines,
    secret_lines,
    store_fingerprint,
)
from alla_skill_lib.sources import check_entry_analysis

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


def project_kb(run: dict[str, Any], entry: dict[str, Any]) -> ProjectKB:
    """База знаний модуля кластера; разбор старой версии без ``kb_dir`` в записи — общая."""
    project_root = Path(run["project_root"])
    directory = Path(entry.get("kb_dir") or run.get("kb_dir") or find_kb_dir(project_root))
    return ProjectKB(directory, kb_label(project_root, directory))


def remember(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    entry_id: str | None,
    today: date,
    *,
    from_analysis: bool = False,
) -> tuple[str, str]:
    """Сохранить причину и рецепт для кластера. Возвращает (статус, текст).

    Источник — ``feedback/NN.md`` со слов пользователя. Разбор модели берётся
    как есть только по явному ``from_analysis`` (пользователь подтвердил его):
    иначе забытый или записанный в другой разбор файл обратной связи незаметно
    превратился бы в «подтверждённый» разбор модели.
    """
    file_id = entry["file_id"]
    blocked = _blocked(paths, entry)
    if blocked:
        return "error", blocked

    feedback_path = paths.feedback(file_id)
    has_feedback = feedback_path.is_file() and bool(feedback_path.read_text(encoding="utf-8").strip())
    if not has_feedback and not from_analysis:
        return "fix", _fix_body(paths, file_id, [
            f"нет файла обратной связи {feedback_path} — запиши его со слов пользователя; "
            "если пользователь подтвердил разбор как есть, повтори команду с --from-analysis"
        ], entry_id)
    # Флаг выбирает источник явно: файл обратной связи мог остаться от прошлого
    # обсуждения и противоречить разбору, который пользователь только что подтвердил.
    from_feedback = has_feedback and not from_analysis
    ignored_feedback = has_feedback and from_analysis
    source = feedback_path if from_feedback else paths.analysis(file_id)
    project_root = Path(run["project_root"])
    if from_feedback:
        # Обратная связь — слова пользователя: наблюдения и «НЕ ХВАТАЕТ» в ней необязательны.
        parsed = parse_analysis(source.read_text(encoding="utf-8"))
    else:
        parsed, analysis_errors = check_entry_analysis(
            source.read_text(encoding="utf-8"), entry, project_root, paths)
        if analysis_errors:
            return "fix", _fix_body(paths, file_id, [
                "разбор кластера не прошёл проверку формата ("
                + "; ".join(analysis_errors[:3]) + ") — analyses/NN.md не переписывай: "
                "спроси пользователя причину и рецепт и запиши их в файл обратной связи"
            ], entry_id)

    kb = project_kb(run, entry)
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
    title = _title(parsed.title or parsed.what or parsed.cause_reason)
    description = " ".join(parsed.cause_reason.split())
    # alla-kb/ коммитится: секретов не должно быть ни в одном сохраняемом поле.
    for field_name, text in (
        ("НАЗВАНИЕ", title),
        ("ПРИЧИНА", description),
        ("КАК ИСПРАВИТЬ", "\n".join(steps)),
    ):
        for line in secret_lines(text):
            errors.append(
                f"в «{field_name}:» строка похожа на секрет, её нельзя коммитить: «{line[:60]}»"
            )
    if errors:
        return "fix", _fix_body(paths, file_id, errors, entry_id)

    assert parsed.category is not None
    signature = str(entry["signature"])
    records, _warnings = kb.load()
    if record is None:
        owner = next((r for r in records if signature in r.confirmed_signatures), None)
        if owner is not None:
            return "error", (
                f"Эту ошибку уже подтверждали в записи «{owner.id}» ({owner.title}). "
                f"Чтобы обновить её: {_update_command(paths, file_id, owner.id, from_analysis)}"
            )
        new_id = make_entry_id(title, fingerprint)
        if kb.path_for(new_id).exists():
            return "error", (
                f"Запись «{new_id}» уже есть. Чтобы обновить её: "
                f"{_update_command(paths, file_id, new_id, from_analysis)}"
            )
        record = KBRecord(
            id=new_id,
            title=title,
            category=CATEGORY_TO_KB[parsed.category],
            description=description,
            resolution_steps=steps,
            error_example=store_fingerprint(fingerprint),
            created={"date": today.isoformat(), "launch_id": run["launch_id"], "cluster": file_id},
        )
        action = "новая запись"
    else:
        if parsed.title:
            record.title = title
        record.category = CATEGORY_TO_KB[parsed.category]
        record.description = description
        record.resolution_steps = steps
        if parsed.fingerprint:
            record.error_example = store_fingerprint(parsed.fingerprint)
        action = "запись обновлена"

    record.confirm(signature)
    changed = [kb.save(record)]
    touched = [record.id]
    for other in records:
        if other.id != record.id and signature in other.confirmed_signatures:
            other.confirmed_signatures.remove(signature)
            changed.append(kb.save(other))
            touched.append(other.id)
    return "saved", "\n".join([
        f"Запомнено в базе знаний ({action}): {record.id} — «{record.title}»",
        *(
            [f"Источник — разбор модели (--from-analysis); файл {feedback_path} не использован."]
            if ignored_feedback else []
        ),
        "Изменены файлы:",
        *(f"- {path}" for path in changed),
        "Скажи пользователю, что рецепт сохранён, и напомни закоммитить папку "
        f"{kb.label}/, чтобы им пользовалась вся команда.",
        *_report_refresh(paths, run, kb, touched),
    ])


def _update_command(paths: ws.RunPaths, file_id: str, entry_id: str, from_analysis: bool) -> str:
    """Команда обновления существующей записи с тем же источником, что выбрал пользователь.

    Без ``--from-analysis`` повтор взял бы ``feedback/NN.md`` (старый или чужой) вместо
    разбора, который пользователь только что подтвердил.
    """
    args = ["remember", file_id, "--entry", entry_id, "--run", str(paths.root)]
    if from_analysis:
        args.append("--from-analysis")
    return ws.skill_command(*args)


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
    kb = project_kb(run, entry)
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
        f"Напомни пользователю закоммитить папку {kb.label}/.",
        *_report_refresh(paths, run, kb, [record.id]),
    ])


def _report_refresh(
    paths: ws.RunPaths,
    run: dict[str, Any],
    kb: ProjectKB,
    entry_ids: list[str],
) -> list[str]:
    """Попросить пересобрать отчёт, если изменённую запись называют разборы этого прогона.

    Известные проблемы отчёта проверяются по текущим файлам базы знаний
    (``known_issues``): после ``reject`` проблема выходит из группы, но только при
    следующей сборке отчёта.
    """
    numbers: list[str] = []
    for item in run["clusters"]:
        directory = Path(item.get("kb_dir") or run.get("kb_dir") or "")
        analysis_path = paths.analysis(item["file_id"])
        if directory != kb.directory or not analysis_path.is_file():
            continue
        if parse_analysis(ws.read_text(analysis_path)).kb_ref in entry_ids:
            numbers.append(str(int(item["file_id"])))
    if not numbers:
        return []
    noun = "проблемы" if len(numbers) == 1 else "проблем"
    return [(
        f"Запись указана в разборах {noun} {', '.join(numbers)} — известные проблемы в отчёте "
        f"изменятся. Выполни {paths.next_command()} и выведи пользователю новый краткий разбор, "
        "как обычно."
    )]


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


def _fix_body(
    paths: ws.RunPaths,
    file_id: str,
    errors: list[str],
    entry_id: str | None,
) -> str:
    # Повтор должен обновить ту же запись: без --entry создалась бы новая
    # (или ответ «уже подтверждали»).
    retry = ["remember", file_id, *(["--entry", entry_id] if entry_id else []), "--run", str(paths.root)]
    return "\n".join([
        "Обратная связь не сохранена:",
        *(f"- {error}" for error in errors),
        f"Запиши или исправь файл: {paths.feedback(file_id)}",
        "Формат:",
        FEEDBACK_FORMAT,
        reference_line(FEEDBACK_FORMAT_REF),
        f"Затем выполни: {ws.skill_command(*retry)}",
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
