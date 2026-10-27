"""Обратная связь пользователя → база знаний проекта: ``remember`` и ``reject``.

Пользователь говорит в чате причину проблемы и рецепт (или подтверждает
разбор). Агент записывает это в ``feedback/NN.md`` и вызывает ``remember``:
скрипт проверяет текст, признак ошибки и сохраняет запись в ``alla-kb/``.
Сигнатура кластера становится подтверждённой — в следующий раз та же ошибка
узнаётся точно. ``reject`` запоминает, что запись к этой ошибке не относится.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.agent_rules import FEEDBACK_FORMAT_REF, record_problem, reference_line
from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.confirmed import drop_confirmed, find_finding, load_finding, save_confirmed
from alla_skill_lib.kb import (
    CATEGORY_TO_KB,
    KB_TO_CATEGORY,
    MAX_FINGERPRINT_LINES,
    KBRecord,
    ProjectKB,
    entry_tests,
    fingerprint_lines,
    kb_label,
    make_entry_id,
    missing_fingerprint_lines,
    normalize_fp,
    record_matches,
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
    """База знаний модуля кластера (``kb_dir`` записи кластера)."""
    project_root = Path(run["project_root"])
    directory = Path(entry["kb_dir"])
    return ProjectKB(directory, kb_label(project_root, directory))


@dataclass
class _Input:
    """Что сохраняется в базу знаний: из обратной связи, разбора модели или вывода расширения."""

    category: str | None
    title: str
    title_given: bool  # название задано явно — им обновляется существующая запись
    description: str
    steps: list[str]
    fingerprint: str  # ПРИЗНАК из обратной связи; пусто — признак кластера


@dataclass
class _Saved:
    """Что сохранил ``remember``: для ответа по одной проблеме и по нескольким."""

    file_id: str
    action: str  # новая запись | запись обновлена | запись подтверждена
    record: KBRecord
    kb: ProjectKB
    changed: list[Path]
    refresh: list[str]
    note: str  # какой источник взят вместо найденного файла обратной связи; пусто — нет


def remember(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    entry_id: str | None,
    today: date,
    *,
    from_analysis: bool = False,
    from_extension: str | None = None,
) -> tuple[str, str]:
    """Сохранить причину и рецепт для кластера. Возвращает (статус, текст).

    Источник — ``feedback/NN.md`` со слов пользователя. Разбор модели берётся
    как есть только по явному ``from_analysis`` (пользователь подтвердил его):
    иначе забытый или записанный в другой разбор файл обратной связи незаметно
    превратился бы в «подтверждённый» разбор модели. ``from_extension`` — вывод
    расширения по проблеме (``confirmed.py``): пользователь подтвердил его, и он
    попадает ещё и в отчёт (``confirmed/NN.json``).
    """
    outcome = _remember(paths, run, entry, entry_id, today, from_analysis=from_analysis,
                        from_extension=from_extension)
    if isinstance(outcome, tuple):
        return outcome
    return "saved", "\n".join([
        f"Запомнено в базе знаний ({outcome.action}): {outcome.record.id} — "
        f"«{outcome.record.title}»",
        *([outcome.note] if outcome.note else []),
        "Изменены файлы:",
        *(f"- {path}" for path in outcome.changed),
        "Скажи пользователю, что рецепт сохранён, и напомни закоммитить папку "
        f"{outcome.kb.label}/, чтобы им пользовалась вся команда.",
        *outcome.refresh,
    ])


def remember_many(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entries: list[dict[str, Any]],
    today: date,
) -> tuple[str, str]:
    """Сохранить подтверждённые пользователем разборы нескольких проблем (``--from-analysis``).

    Ответ после отчёта «верны 1, 2 и 7» — одна команда, а не по команде на номер. Каждая
    проблема сохраняется как своим ``remember``; отказ по одной не мешает остальным, а
    ``next`` просится один раз в конце. Не сохранилось ничего — ответ как у одной проблемы.
    """
    saved: list[_Saved] = []
    refused: list[tuple[str, str, str]] = []  # номер, статус, текст отказа
    for entry in entries:
        outcome = _remember(paths, run, entry, None, today, from_analysis=True)
        if isinstance(outcome, tuple):
            refused.append((entry["file_id"], *outcome))
        else:
            saved.append(outcome)
    if not saved and len(refused) == 1:
        return refused[0][1], refused[0][2]
    numbers = ", ".join(str(int(file_id)) for file_id, _, _ in refused)
    # Замечания — без команд из отказа по одной проблеме: их «Затем выполни» модель выполнила
    # бы все подряд или написала бы файл обратной связи из своего разбора (ревью исполнителя).
    not_saved = [
        f"Не сохранены проблемы {numbers}. Покажи пользователю замечания как есть; сохранить "
        "такую проблему можно только со слов пользователя — файл обратной связи и remember N "
        "по одной (без --from-analysis), когда он назовёт причину и рецепт. Сам команд по ним "
        "не выполняй.",
        *(f"- Проблема {int(file_id)}: {_refusal_reason(text)}" for file_id, _, text in refused),
    ]
    if not saved:
        return "error", "\n".join(not_saved)
    labels = list(dict.fromkeys(outcome.kb.label for outcome in saved))
    lines = [
        f"Запомнено в базе знаний проблем: {len(saved)} из {len(entries)}.",
        *(f"- Проблема {int(outcome.file_id)} ({outcome.action}): {outcome.record.id} — "
          f"«{outcome.record.title}»" for outcome in saved),
        *[outcome.note for outcome in saved if outcome.note],
        "Изменены файлы:",
        *(f"- {path}" for path in dict.fromkeys(path for outcome in saved for path in outcome.changed)),
        "Скажи пользователю, что разборы сохранены, и напомни закоммитить "
        + ("папку " if len(labels) == 1 else "папки ") + ", ".join(f"{label}/" for label in labels)
        + ", чтобы ими пользовалась вся команда.",
    ]
    if refused:
        lines += not_saved
    if any(outcome.refresh for outcome in saved):
        lines.append(
            f"Отчёт изменился. Выполни {paths.next_command()} и дальше как обычно: попросит "
            "сводку — напиши её заново, ответит done — выведи пользователю новый краткий разбор "
            "дословно."
        )
    return "saved", "\n".join(lines)


def _remember(
    paths: ws.RunPaths,
    run: dict[str, Any],
    entry: dict[str, Any],
    entry_id: str | None,
    today: date,
    *,
    from_analysis: bool = False,
    from_extension: str | None = None,
) -> tuple[str, str] | _Saved:
    """Общий путь ``remember``: отказ (статус, текст) или что сохранено."""
    file_id = entry["file_id"]
    blocked = _blocked(paths, entry)
    if blocked:
        return "error", blocked

    feedback_path = paths.feedback(file_id)
    has_feedback = feedback_path.is_file() and bool(feedback_path.read_text(encoding="utf-8").strip())
    finding: dict[str, Any] | None = None
    confirm_ref: str | None = None  # пользователь подтвердил разбор со ссылкой на запись R
    if from_extension:
        finding_path = find_finding(paths, from_extension, file_id)
        if finding_path is None:
            return "error", (
                f"У расширения {from_extension} нет вывода по проблеме №{int(file_id)} в "
                f"{paths.extension_dir(from_extension)} — подтверждать нечего. Скажи это "
                "пользователю."
            )
        finding, finding_errors = load_finding(finding_path, file_id)
        if finding_errors:
            return "error", "\n".join([
                f"Вывод расширения {from_extension} по проблеме №{int(file_id)} сохранить нельзя:",
                *(f"- {error}" for error in finding_errors),
                "Покажи пользователю эти замечания как есть; remember не повторяй.",
            ])
        given = _Input(
            category=str(finding["category"]),
            title=_title(str(finding["title"])),
            title_given=True,
            description=" ".join(str(finding["cause"]).split()),
            steps=[" ".join(step.split()) for step in finding["fix"]],
            fingerprint="",
        )
    else:
        if not has_feedback and not from_analysis:
            return _refused(paths, file_id, [
                f"нет файла обратной связи {feedback_path} — запиши его со слов пользователя; "
                "если пользователь подтвердил разбор как есть, повтори команду с --from-analysis"
            ], entry_id)
        # Флаг выбирает источник явно: файл обратной связи мог остаться от прошлого
        # обсуждения и противоречить разбору, который пользователь только что подтвердил.
        from_feedback = has_feedback and not from_analysis
        source = feedback_path if from_feedback else paths.analysis(file_id)
        if not source.is_file():
            # Разбор ещё не написан: подтверждать нечего (и пакет remember не обрывается).
            return "error", (f"У проблемы №{int(file_id)} ещё нет разбора агента — подтверждать "
                             "нечего. Скажи это пользователю.")
        project_root = Path(run["project_root"])
        if from_feedback:
            # Обратная связь — слова пользователя: наблюдения и «НЕ ХВАТАЕТ» в ней необязательны.
            parsed = parse_analysis(source.read_text(encoding="utf-8"))
        else:
            parsed, analysis_errors = check_entry_analysis(
                source.read_text(encoding="utf-8"), entry, project_root, paths)
            if analysis_errors:
                return _refused(paths, file_id, [
                    "разбор кластера не прошёл проверку формата ("
                    + "; ".join(analysis_errors[:3]) + ") — analyses/NN.md не переписывай: "
                    "спроси пользователя причину и рецепт и запиши их в файл обратной связи"
                ], entry_id)
        given = _Input(
            category=parsed.category,
            title=_title(parsed.title or parsed.what or parsed.cause_reason),
            title_given=bool(parsed.title),
            description=" ".join(parsed.cause_reason.split()),
            steps=[_step_text(line) for line in parsed.fix.splitlines() if _step_text(line)],
            fingerprint=parsed.fingerprint,
        )
        if not from_feedback and parsed.kb_ref and entry_id in (None, parsed.kb_ref):
            # Разбор назвал известную проблему, пользователь его подтвердил: подтверждается
            # эта запись, а не создаётся её дубль из слов разбора.
            confirm_ref = parsed.kb_ref
    ignored_feedback = has_feedback and (from_analysis or bool(from_extension))
    source_args = (["--from-extension", from_extension] if from_extension
                   else ["--from-analysis"] if from_analysis else [])

    kb = project_kb(run, entry)
    confirmed: KBRecord | None = None
    if confirm_ref is not None:
        try:
            confirmed = kb.get(confirm_ref)
        except ValueError as exc:
            return "error", str(exc)
        # Записи уже нет — сохраняется сам разбор, как без ссылки.
        if confirmed is not None and KB_TO_CATEGORY.get(confirmed.category) != given.category:
            return _refused(paths, file_id, [
                f"разбор ссылается на известную проблему «{confirmed.id}» с категорией "
                f"«{KB_TO_CATEGORY.get(confirmed.category)}», а сам называет «{given.category}» — "
                "спроси пользователя, какая причина верна, и запиши её в файл обратной связи"
            ], entry_id)
    record: KBRecord | None = None
    if entry_id:
        try:
            record = kb.get(entry_id)
        except ValueError as exc:
            return "error", str(exc)
        if record is None:
            return "error", f"В {kb.directory} нет записи «{entry_id}»."

    errors: list[str] = []
    fingerprint = title = description = ""
    steps: list[str] = []
    # Подтверждается известная запись как есть: её текст не меняется, проверять нечего.
    if confirmed is None:
        if given.category is None or given.category == "неизвестно":
            errors.append(
                "в «ПРИЧИНА:» первым словом нужна категория тест / приложение / окружение / "
                "данные («неизвестно» запоминать нельзя)"
            )
        steps = given.steps
        if not steps:
            errors.append("нет «КАК ИСПРАВИТЬ:» — рецепт обязателен")
        if record is None or given.fingerprint:
            # Признак проверяется, только когда он сохраняется: у обновляемой
            # записи без нового ПРИЗНАКА эту ошибку узнает подтверждённая сигнатура.
            fingerprint = given.fingerprint or str(entry.get("fingerprint") or "")
            errors.extend(_fingerprint_errors(fingerprint, paths.evidence(file_id)))
        else:
            fingerprint = record.error_example
        title = given.title
        description = given.description
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
    if errors and from_extension:
        # Вывод расширения модель не перепишет: отказ — сразу пользователю. Признак у этого
        # пути не дописать (feedback/NN.md не читается) — говорим об этом прямо.
        errors = ["у проблемы нет признака ошибки, по которому база знаний узнает её снова — "
                  "вывод не сохранён; пользователь может назвать причину сам (обычная обратная "
                  "связь с ПРИЗНАКОМ)" if "признак" in error.lower() else error
                  for error in errors]
        return "error", "\n".join([
            f"Вывод расширения {from_extension} по проблеме №{int(file_id)} в базу знаний не "
            "сохранён:", *(f"- {error}" for error in errors),
            "Покажи пользователю эти замечания как есть; remember не повторяй.",
        ])
    if errors:
        return _refused(paths, file_id, errors, entry_id)

    assert given.category is not None
    signature = str(entry["signature"])
    tests = entry_tests(run, entry)
    haystack = normalize_fp(ws.read_text(paths.evidence(file_id)))

    def confirmed_here(candidate: KBRecord) -> bool:
        """Запись уже точно узнаёт эту ошибку: по сигнатуре или, после смены версии, по тесту."""
        return record_matches(candidate, signature, haystack, tests) == "exact"

    records, _warnings = kb.load()
    if confirmed is not None:
        record = confirmed
        action = "запись подтверждена"
    elif record is None:
        owner = next((r for r in records if confirmed_here(r)), None)
        if owner is not None:
            # Своя причина пользователя — не уточнение записи: обновление переписало бы её и
            # для других проблем, которые она узнаёт (ревью исполнителя). У вывода расширения
            # путь прежний — SKILL.md велит обновить запись готовой командой.
            other_cause = "" if from_extension else (
                "\nОбновляй, только если пользователь уточняет рецепт этой же записи. Говорит, что "
                "причина другая, — сначала "
                + ws.skill_command("reject", file_id, owner.id, "--run", str(paths.root))
                + ", затем повтори remember без --entry: обновление переписало бы запись и для "
                "других проблем, которые она узнаёт."
            )
            return "error", (
                f"Эту ошибку уже подтверждали в записи «{owner.id}» ({owner.title}). "
                f"{_UPDATE_PREFIX}{_update_command(paths, file_id, owner.id, source_args)}"
                + other_cause
            )
        new_id = make_entry_id(title, fingerprint)
        if kb.path_for(new_id).exists():
            return "error", (
                f"Запись «{new_id}» уже есть. "
                f"{_UPDATE_PREFIX}{_update_command(paths, file_id, new_id, source_args)}"
            )
        record = KBRecord(
            id=new_id,
            title=title,
            category=CATEGORY_TO_KB[given.category],
            description=description,
            resolution_steps=steps,
            error_example=store_fingerprint(fingerprint),
            created={"date": today.isoformat(), "launch_id": run["launch_id"], "cluster": file_id},
        )
        action = "новая запись"
    else:
        if given.title_given:
            record.title = title
        record.category = CATEGORY_TO_KB[given.category]
        record.description = description
        record.resolution_steps = steps
        if given.fingerprint:
            record.error_example = store_fingerprint(given.fingerprint)
        action = "запись обновлена"
    if finding is not None and from_extension:
        # Детали вывода (ревизия, место в коде, коммит) — своим полем записи расширения.
        record.extra[from_extension] = {
            **finding.get("kb_fields", {}),
            "confirmed": today.isoformat(),
            "launch_id": run["launch_id"],
        }

    record.confirm(signature, tests, [str(entry["log_digest"])] if entry.get("log_digest") else [])
    changed = [kb.save(record)]
    touched = [record.id]
    for other in records:
        if other.id != record.id and confirmed_here(other):
            # Ошибку переназначили этой записи: прежняя теряет и сигнатуру, и тесты кластера —
            # иначе она узнавалась бы по ним точно (и та, что узнавала её по тесту после
            # смены версии, — ревью Codex).
            other.confirmed_signatures = [s for s in other.confirmed_signatures if s != signature]
            other.confirmed_tests = sorted(set(other.confirmed_tests) - set(tests))
            changed.append(kb.save(other))
            touched.append(other.id)
    _count_refusal(paths, file_id, reset=True)
    refresh = _report_refresh(paths, run, kb, touched)
    if finding is not None and from_extension:
        changed.append(save_confirmed(paths, file_id, from_extension, finding, record.id, today))
        refresh = [
            f"Вывод добавлен в отчёт у проблемы {int(file_id)}. Выполни {paths.next_command()} "
            "и дальше как обычно: попросит сводку — напиши её заново, ответит done — выведи "
            "пользователю новый краткий разбор дословно."
        ]
    elif (dropped := drop_confirmed(paths, file_id)) is not None:
        # Пользователь поправил причину: прежний вывод расширения больше не подтверждён.
        changed.append(dropped)
        refresh = [_confirmed_dropped(paths, file_id)]
    source_note = (
        f"Источник — вывод расширения {from_extension}" if from_extension
        else "Источник — разбор модели (--from-analysis)"
    )
    return _Saved(
        file_id=file_id,
        action=action,
        record=record,
        kb=kb,
        changed=changed,
        refresh=refresh,
        note=f"{source_note}; файл {feedback_path} не использован." if ignored_feedback else "",
    )


def _refusal_reason(text: str) -> str:
    """Суть отказа одной строкой: его замечания («- …») или первая строка без команды
    обновления записи — её пакет не передаёт (ревью Codex): без строки «обновляй, только если
    пользователь уточняет рецепт» модель выполнила бы её и переписала запись."""
    reasons = [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]
    if reasons:
        return "; ".join(reasons)
    first = text.splitlines()[0] if text.strip() else ""
    first = first.split(_UPDATE_PREFIX, 1)[0].rstrip()
    return first.removeprefix("Попытка ").split(". ", 1)[-1] if first.startswith("Попытка ") else first


# Перед командой обновления существующей записи: отказ пакета remember её вырезает.
_UPDATE_PREFIX = "Чтобы обновить её: "


def _update_command(paths: ws.RunPaths, file_id: str, entry_id: str, source_args: list[str]) -> str:
    """Команда обновления существующей записи с тем же источником, что выбрал пользователь.

    Без ``--from-analysis`` (``--from-extension``) повтор взял бы ``feedback/NN.md`` (старый
    или чужой) вместо разбора, который пользователь только что подтвердил.
    """
    args = ["remember", file_id, "--entry", entry_id, "--run", str(paths.root), *source_args]
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
    record.reject(str(entry["signature"]), entry_tests(run, entry))
    path = kb.save(record)
    refresh = _report_refresh(paths, run, kb, [record.id])
    # Подтверждение, сохранённое в эту запись, отвергнуто вместе с ней.
    dropped = drop_confirmed(paths, entry["file_id"], record.id)
    if dropped is not None:
        refresh = [_confirmed_dropped(paths, entry["file_id"])]
    return "saved", "\n".join([
        f"Запись {record.id} больше не будет предлагаться для этой ошибки.",
        f"Изменён файл: {path}",
        *([f"Удалён файл: {dropped}"] if dropped is not None else []),
        f"Напомни пользователю закоммитить папку {kb.label}/.",
        *refresh,
    ])


def _confirmed_dropped(paths: ws.RunPaths, file_id: str) -> str:
    return (
        f"Прежний подтверждённый вывод расширения по проблеме {int(file_id)} снят — в отчёте "
        f"его больше не будет. Выполни {paths.next_command()} и дальше как обычно: попросит "
        "сводку — напиши её заново, ответит done — выведи пользователю новый краткий разбор "
        "дословно."
    )


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
        directory = Path(item["kb_dir"])
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
        f"изменятся. Выполни {paths.next_command()} и дальше как обычно: попросит разобрать "
        "проблему (её разбирали по этой записи) — разбери, как в цикле; ответит done — выведи "
        "пользователю новый краткий разбор."
    )]


def _blocked(paths: ws.RunPaths, entry: dict[str, Any]) -> str | None:
    if entry.get("auto") or not entry.get("signature"):
        return "У этого кластера нет данных об ошибке (сообщения, трейса, лога) — запоминать нечего."
    if not paths.evidence(entry["file_id"]).is_file():
        return (f"Нет {paths.evidence(entry['file_id']).name} — папка разбора повреждена: "
                "выполни prepare заново с --fresh, затем повтори.")
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


# Отказов remember подряд по одной проблеме, после которых он останавливает модель (error):
# сама она отказы не сосчитает, а лимита попыток, как у next, у remember нет.
MAX_REFUSALS = 3


def _count_refusal(paths: ws.RunPaths, file_id: str, *, reset: bool = False) -> int:
    """Учесть отказ ``remember`` (или сбросить счёт); вернуть номер отказа подряд."""
    try:
        loaded = ws.read_json(paths.state_json) if paths.state_json.is_file() else {}
    except (OSError, ValueError):
        loaded = {}
    state: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
    refusals = state.setdefault("refusals", {})
    if reset:
        if refusals.pop(file_id, None) is not None:
            ws.write_json(paths.state_json, state)
        return 0
    count = int(refusals.get(file_id, 0)) + 1
    if count >= MAX_REFUSALS:
        refusals.pop(file_id, None)  # следующая просьба пользователя — новые попытки
    else:
        refusals[file_id] = count
    ws.write_json(paths.state_json, state)
    return count


def _refused(
    paths: ws.RunPaths,
    file_id: str,
    errors: list[str],
    entry_id: str | None,
) -> tuple[str, str]:
    count = _count_refusal(paths, file_id)
    mismatch = (f"Если файл, по-твоему, верен, а проверка его отклоняет, — это неполадка скилла: "
                f"{record_problem(paths)}.")
    if count >= MAX_REFUSALS:
        return "error", "\n".join([
            f"Обратная связь не сохранена: проверка отклонила файл {MAX_REFUSALS} раза подряд.",
            *(f"- {error}" for error in errors),
            "Покажи пользователю эти замечания как есть и скажи, что сохранить в базу знаний не "
            "удалось; remember не повторяй, пока пользователь не уточнит причину или рецепт.",
            mismatch,
        ])
    return "fix", "\n".join([
        f"Попытка {count} из {MAX_REFUSALS}. " + _fix_body(paths, file_id, errors, entry_id),
        mismatch,
    ])


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
