"""Выводы расширений по проблемам и их подтверждение пользователем.

Расширение (например, ``sut-code`` — разбор по коду приложения) может оставить по проблеме
вывод: ``<папка разбора>/extensions/<имя>/<stem>.finding.json``, где ``<stem>`` кончается
номером проблемы (``on_request-03``). Формат — контракт расширений (``extensions/README.md``,
«Вывод по проблеме»). Сам по себе вывод в отчёт и базу знаний не попадает: только после
«да, верно» пользователя ``remember N --from-extension <имя>`` сохраняет его в базу знаний
и пишет ``confirmed/NN.json`` — его показывает отчёт.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.kb import CATEGORY_TO_KB, secret_lines

FINDING_SUFFIX = ".finding.json"
MAX_DETAILS = 8
MAX_DETAIL_CHARS = 400
MAX_TEXT_CHARS = 1000
MAX_FIX_STEPS = 10
MAX_KB_FIELDS = 10
_KB_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def find_finding(paths: ws.RunPaths, extension: str, file_id: str) -> Path | None:
    """Последний вывод расширения по проблеме (``*-NN.finding.json``)."""
    folder = paths.extension_dir(extension)
    found = [path for path in folder.glob(f"*-{file_id}{FINDING_SUFFIX}") if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime) if found else None


def check_finding(data: Any, file_id: str) -> list[str]:
    """Что не так с выводом расширения (пусто — годится для отчёта и базы знаний)."""
    if not isinstance(data, dict):
        return ["вывод — не JSON-объект"]
    errors: list[str] = []
    if data.get("problem") != int(file_id):
        errors.append(f"вывод относится к проблеме {data.get('problem')!r}, а не {int(file_id)}")
    if data.get("format_ok") is not True:
        errors.append("разбор расширения не прошёл его проверку формата")
    category = data.get("category")
    if category not in CATEGORY_TO_KB:
        errors.append(f"категория {category!r}: сохранить можно только тест, приложение, "
                      "окружение или данные")
    for key in ("title", "cause"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
            errors.append(f"поле {key} — непустая строка до {MAX_TEXT_CHARS} символов")
    fix = data.get("fix")
    if (not isinstance(fix, list) or not fix or len(fix) > MAX_FIX_STEPS
            or not all(isinstance(step, str) and step.strip() for step in fix)):
        errors.append(f"поле fix — от 1 до {MAX_FIX_STEPS} шагов-строк")
    details = data.get("details", [])
    if not isinstance(details, list) or len(details) > MAX_DETAILS or not all(
            isinstance(row, str) and len(row) <= MAX_DETAIL_CHARS for row in details):
        errors.append(f"поле details — до {MAX_DETAILS} строк по {MAX_DETAIL_CHARS} символов")
    fields = data.get("kb_fields", {})
    if not isinstance(fields, dict) or len(fields) > MAX_KB_FIELDS or not all(
            isinstance(key, str) and _KB_KEY_RE.fullmatch(key) and isinstance(value, str)
            and len(value) <= MAX_DETAIL_CHARS for key, value in fields.items()):
        errors.append(f"поле kb_fields — до {MAX_KB_FIELDS} пар «ключ латиницей: строка»")
    if errors:
        return errors
    # alla-kb/ и отчёт коммитятся и пересылаются: секретов не должно быть ни в одном поле.
    texts = [data["title"], data["cause"], *data["fix"], *data.get("details", []),
             *data.get("kb_fields", {}).values()]
    for line in secret_lines("\n".join(texts)):
        errors.append(f"строка похожа на секрет, её нельзя сохранять: «{line[:60]}»")
    return errors


def load_finding(path: Path, file_id: str) -> tuple[dict[str, Any], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"{path.name} не читается: {exc}"]
    errors = check_finding(data, file_id)
    return (data if not errors else {}), errors


def save_confirmed(
    paths: ws.RunPaths,
    file_id: str,
    extension: str,
    finding: dict[str, Any],
    record_id: str,
    today: date,
) -> Path:
    path = paths.confirmed(file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ws.write_json(path, {
        "extension": extension,
        "confirmed": today.isoformat(),
        "kb_entry": record_id,
        **{key: finding[key] for key in ("category", "title", "cause", "fix")},
        # details по контракту необязательны (check_finding): нет — пустой список.
        "details": finding.get("details", []),
    })
    return path


def drop_confirmed(paths: ws.RunPaths, file_id: str, kb_entry: str | None = None) -> Path | None:
    """Снять подтверждение проблемы (путь удалённого файла или None, если снимать нечего).

    Зовут ``remember`` без ``--from-extension`` (пользователь поправил причину — прежний
    вывод больше не подтверждён) и ``reject`` (с ``kb_entry``: только если подтверждение
    сохранено в эту запись).
    """
    path = paths.confirmed(file_id)
    if not path.is_file():
        return None
    if kb_entry is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("kb_entry") != kb_entry:
            return None
    path.unlink()
    return path


def load_confirmed(paths: ws.RunPaths, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Подтверждённые выводы расширений по номерам проблем (битые файлы пропускаются)."""
    result: dict[str, dict[str, Any]] = {}
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        path = paths.confirmed(file_id)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(data, dict) and data.get("category") in CATEGORY_TO_KB
                and isinstance(data.get("cause"), str) and isinstance(data.get("fix"), list)
                and isinstance(data.get("details", []), list)):
            result[file_id] = data
    return result
