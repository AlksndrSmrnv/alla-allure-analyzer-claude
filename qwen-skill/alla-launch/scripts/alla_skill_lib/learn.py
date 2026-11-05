"""Что разбор сам запоминает в базе знаний по итогу перезапуска — без отдельного вопроса.

Решение пользователя (2026-11-05): проверенное фактами пишется сразу, а пользователь узнаёт
об этом из ответа и из diff ``alla-kb/`` при коммите. Проверено фактами:

- отметка «нестабильный тест» (``flaky.remember_plan``: прошёл на коде как в CI при признаке
  из CI; снять — отмеченный тест упал все попытки той же ошибкой);
- проверенная правка агента (``fix_facts``): «да» на diff и тесты проблемы прошли с первого
  запуска.

Догадки модели сюда не попадают: молчание — не согласие. Статус в TestOps — по-прежнему только
после «да» (``rerun --testops``). Запоминается один раз на задание: повторный ``--wait`` или
сборка отчёта показывают уже записанное (``rerun/learned.json``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from alla_skill_lib import fix_facts, flaky, rerun
from alla_skill_lib import workspace as ws
from alla_skill_lib.kb import kb_label


@dataclass
class Learned:
    """Записанное по итогу задания: строки для ответа и папки базы знаний."""

    lines: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    changed: bool = False  # записано сейчас (а не прочитано из ``learned.json``)


def learn_from_rerun(paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState) -> Learned:
    """Записать отметки «нестабильный» и проверенные правки по законченному заданию — один раз."""
    if state.state != "done" or state.result is None or state.plan is None:
        return Learned()
    files = rerun.job_files(paths)
    job = str(state.result.get("job"))
    done = rerun.read_learned(files, job)
    if done is not None:
        return Learned(list(done.get("lines") or []), list(done.get("folders") or []))
    today = date.today().isoformat()
    saved: list[tuple[Path, bytes | None]] = []
    try:
        lines, folders = _flaky(run, state, today, saved)
        fix_lines, fix_folders = fix_facts.record_checked(paths, run, state, today, saved)
    except OSError as exc:
        # Итог перезапуска важнее: ошибка записи его не прячет, а следующий показ повторит запись.
        # Факты этой попытки откатываются к прежним: к следующему показу условия могут уже не
        # выполняться (ревью Grok), а факт прошлых прогонов терять нельзя (ревью Codex);
        # отметки «нестабильный» — тоже (снятая иначе пропала бы без уведомления, ревью Codex).
        _restore(saved)
        return Learned([f"В базу знаний итог не записан: {exc}. Скажи это пользователю."])
    lines += fix_lines
    folders = sorted({*folders, *fix_folders})
    try:
        rerun.write_learned(files, job, lines, folders)
    except OSError:
        pass  # записанное в базе знаний есть; следующий показ запишет его заново (то же)
    return Learned(lines, folders, changed=bool(folders))


def _restore(saved: list[tuple[Path, bytes | None]]) -> None:
    for path, previous in reversed(saved):
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                ws.write_atomic_bytes(path, previous)
        except OSError:
            continue


def _flaky(run: dict[str, Any], state: rerun.JobState, today: str,
           saved: list[tuple[Path, bytes | None]]) -> tuple[list[str], list[str]]:
    """Отметки «нестабильный» в ``alla-kb/flaky/``; строки ответа и папки базы знаний.

    ``saved`` получает прежнее содержимое каждого файла, который может измениться, — для отката.
    """
    assert state.result is not None and state.plan is not None
    keep = flaky.remember_plan(state.result, state.plan, flaky.marks_for_run(run))
    if not keep:
        return [], []
    keys = flaky.keys_of_tests(run)
    entries = {entry["file_id"]: entry for entry in run["clusters"]}
    # Отметка — на метод (параметры в ключ не входят): прошёл один вариант метода, а другой в
    # другой проблеме воспроизвёлся — отметка остаётся, снимать её сразу после записи нельзя
    # (ревью Grok).
    marked_keys = {keys.get(int(item["test_result_id"])) for item in keep.mark}
    keep = flaky.RememberPlan(keep.mark, [item for item in keep.unmark
                                          if keys.get(int(item["test_result_id"])) not in marked_keys])
    project_root = Path(run["project_root"])
    marked: list[str] = []
    unmarked: list[str] = []
    folders: set[str] = set()
    for item in [*keep.mark, *keep.unmark]:
        entry = entries.get(str(item.get("file_id")))
        key = keys.get(int(item["test_result_id"]))
        if entry is None or not key or not entry.get("kb_dir"):
            continue
        store = flaky.FlakyStore(Path(entry["kb_dir"]))
        _remember_before(store, key, saved)
        if item in keep.mark:
            store.mark(key, {"launch_id": run["launch_id"], "passed_on": item.get("attempt"),
                             "attempts": state.result.get("attempts"),
                             "signals": item.get("signals") or []}, today)
            marked.append(str(item["full_name"]))
        elif store.unmark(key):
            unmarked.append(str(item["full_name"]))
        else:
            continue
        folders.add(kb_label(project_root, Path(entry["kb_dir"])))
    lines = []
    if marked:
        lines.append("Отмечены в базе знаний нестабильными: " + "; ".join(marked) + ".")
    if unmarked:
        lines.append("Сняты в базе знаний отметки «нестабильный» (тест воспроизводится каждый "
                     "раз): " + "; ".join(unmarked) + ".")
    return lines, sorted(folders)


def _remember_before(store: flaky.FlakyStore, key: str,
                     saved: list[tuple[Path, bytes | None]]) -> None:
    """Прежнее содержимое файлов отметки теста ``key`` (новой — ``None``)."""
    paths = {store.path_for(key)}
    if store.folder.is_dir():
        for path in store.folder.glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8-sig")).get("test") == key:
                    paths.add(path)
            except (OSError, ValueError, AttributeError):
                continue
    for path in sorted(paths):
        saved.append((path, path.read_bytes() if path.is_file() else None))


def render(learned: Learned) -> str:
    """Строки ответа ``rerun_done``: что записано без вопроса и что сказать пользователю."""
    if not learned.lines:
        return ""
    if not learned.folders:
        return "\n".join(learned.lines)
    shown = ", ".join(learned.folders)
    return "\n".join([
        "Записано в базу знаний без вопроса (это проверенный итог перезапуска, а не догадка "
        "агента):",
        *(f"- {line}" for line in learned.lines),
        f"Скажи это пользователю одной-двумя фразами и напомни закоммитить папку базы знаний "
        f"{shown}. Спрашивать, сохранять ли это, не нужно: уже сохранено. Пользователь не "
        "согласен — назови ему папку (flaky/ или fixes/ в ней): файл он удаляет сам, ты файлы "
        "базы знаний не трогаешь.",
    ])
