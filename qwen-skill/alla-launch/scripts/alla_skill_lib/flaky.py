"""Тесты, похожие на нестабильные (флаки): кого предложить перезапустить локально.

Признаки — по тесту, детерминированно:

* сильные — повтор в TestOps прошёл или упал с другой ошибкой (``retries.retry_group``);
* средний — сообщение похоже на временный сбой (таймаут, обрыв соединения, 502/503/504,
  «stale element»), если разбор не «тест»/«данные» и повторы не «все упали одинаково».

«Окружение» в разборе само по себе не признак: стенд лежал в CI и поднялся — тест пройдёт,
но он не нестабильный. Не предлагаются проблемы с подтверждённым выводом расширения и с
правкой агента — предложенной или применённой (дефект теста; перезапуск — проверка правки) —
и тесты, у которых все повторы в TestOps упали одинаково.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.kb import README, key_of_test
from alla_skill_lib.rerun import failed_tests_of
from alla_skill_lib.retries import retry_group

# Текст сообщения, а не класс исключения: в сообщении Allure класса обычно нет.
TRANSIENT_RE = re.compile(
    r"""
      expected\ condition\ failed:\ waiting\ for     # Selenium WebDriverWait
    | tried\ for\ \d+\ second
    | read\ timed\ out | connect(?:ion)?\ timed\ out | timed\ out\ after | timeout\ exceeded
    | sockettimeoutexception | (?:read|connect)timeout
    | connection\ reset | broken\ pipe
    | stale\ element\ reference | element\ click\ intercepted
    | gateway\ time-?out | service\ unavailable | bad\ gateway
    | (?:status(?:\ code)?[:=\ ]\s*|but\ was:?\ <?|http/\d(?:\.\d)?\ )50[234]\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
STRONG = frozenset({"kb", "retry_passed", "retry_different"})
# Категории, при которых временный сбой в сообщении — не довод: ошибка теста или данных.
STABLE_CATEGORIES = frozenset({"тест", "данные"})


def transient(message: str) -> bool:
    return bool(TRANSIENT_RE.search(message))


def signals_of(test: Mapping[str, Any], category: str | None, *, marked: bool = False) -> list[str]:
    """Признаки нестабильности теста (коды ``rerun.SIGNALS``), сильные первыми.

    ``marked`` — тест отмечен нестабильным в базе знаний: это признак, даже если все повторы в
    TestOps упали одинаково.
    """
    group = retry_group(test)
    signals = ["kb"] if marked else []
    if group == "passed":
        signals.append("retry_passed")
    elif group == "different":
        signals.append("retry_different")
    if (category not in STABLE_CATEGORIES and group != "same"
            and transient(str(test.get("status_message") or ""))):
        signals.append("transient")
    return signals


def candidates(
    run: dict[str, Any],
    categories: Mapping[str, str | None],
    excluded: Iterable[str],
    marked: Iterable[int] = (),
) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    """(запись кластера, тест, признаки): сначала с сильными признаками, затем крупные проблемы.

    ``marked`` — id результатов тестов, отмеченных нестабильными в базе знаний.
    """
    skip = set(excluded)
    flagged = set(marked)
    found: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    for entry in run["clusters"]:
        if entry["file_id"] in skip:
            continue
        for test in failed_tests_of(run, entry):
            signals = signals_of(test, categories.get(entry["file_id"]),
                                 marked=int(test["test_result_id"]) in flagged)
            if signals:
                found.append((entry, test, signals))
    found.sort(key=lambda item: (not STRONG.intersection(item[2]), -int(item[0].get("member_count") or 0)))
    return found


# ---------------------------------------------------------------------------
# отметки в базе знаний: ``<alla-kb>/flaky/``
# ---------------------------------------------------------------------------

FLAKY_DIRNAME = "flaky"
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


class FlakyStore:
    """Отметки «нестабильный тест» в папке ``flaky/`` базы знаний: одна отметка — один файл.

    Записи базы знаний (``alla-kb/*.json``) читаются без подпапок, поэтому отметки им не
    мешают. Свои поля файла (``owner``, ``jira``) сохраняются при обновлении.
    """

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir
        self.folder = kb_dir / FLAKY_DIRNAME

    def path_for(self, key: str) -> Path:
        tail = "_".join(key.replace("#", ".").split(".")[-2:])
        slug = _SLUG_RE.sub("_", tail).strip("_")[:60] or "test"
        return self.folder / f"{slug}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}.json"

    def load(self) -> dict[str, dict[str, Any]]:
        """Отметки по ключу теста; битый файл пропускается."""
        marks: dict[str, dict[str, Any]] = {}
        if not self.folder.is_dir():
            return marks
        for path in sorted(self.folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("test"), str):
                marks[data["test"]] = data
        return marks

    def mark(self, key: str, fields: dict[str, Any], today: str) -> Path:
        path = self.path_for(key)
        current = self.load().get(key, {})
        data = {**current, **fields, "test": key, "since": current.get("since") or today,
                "seen": today}
        self.folder.mkdir(parents=True, exist_ok=True)
        readme = self.kb_dir / "README.md"
        if not readme.exists():
            ws.write_atomic(readme, README)
        ws.write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return path

    def unmark(self, key: str) -> bool:
        removed = False
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("test") == key:
                path.unlink()
                removed = True
        return removed


def keys_of_tests(run: dict[str, Any]) -> dict[int, str]:
    """Ключ базы знаний (``kb.key_of_test``) каждого упавшего теста прогона."""
    keys: dict[int, str] = {}
    for test in (run.get("triage") or {}).get("failed_tests", []):
        key = key_of_test(test.get("full_name"), test.get("test_case_id"))
        if key:
            keys[int(test["test_result_id"])] = key
    return keys


def marks_for_run(run: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Отметки «нестабильный» тестов прогона — по базе знаний модуля каждой проблемы."""
    keys = keys_of_tests(run)
    stores: dict[str, dict[str, dict[str, Any]]] = {}
    found: dict[int, dict[str, Any]] = {}
    for entry in run["clusters"]:
        kb_dir = str(entry.get("kb_dir") or "")
        if not kb_dir:
            continue
        if kb_dir not in stores:
            stores[kb_dir] = FlakyStore(Path(kb_dir)).load()
        for test in failed_tests_of(run, entry):
            test_id = int(test["test_result_id"])
            mark = stores[kb_dir].get(keys.get(test_id, ""))
            if mark is not None:
                found[test_id] = mark
    return found


def marked_by_problem(run: dict[str, Any], marks: dict[int, dict[str, Any]]) -> dict[str, list[str]]:
    """Для отчёта: отмеченные тесты проблемы строками «имя (с даты)»."""
    shown: dict[str, list[str]] = {}
    for entry in run["clusters"]:
        for test in failed_tests_of(run, entry):
            mark = marks.get(int(test["test_result_id"]))
            if mark is not None:
                name = str(test.get("full_name") or test.get("name") or "")
                since = f" (с {mark['since']})" if mark.get("since") else ""
                shown.setdefault(entry["file_id"], []).append(name + since)
    return shown


@dataclass(frozen=True)
class RememberPlan:
    """Что сохранить в базу знаний после перезапуска (``rerun --remember``)."""

    mark: list[dict[str, Any]]  # итоги «прошёл» с признаком из CI, на коде как в CI
    unmark: list[dict[str, Any]]  # отмеченные тесты, упавшие каждый раз той же ошибкой

    def __bool__(self) -> bool:
        return bool(self.mark or self.unmark)


def remember_plan(
    result: dict[str, Any], plan: dict[str, Any], marks: dict[int, dict[str, Any]],
) -> RememberPlan:
    """Разложить итог перезапуска: отметить, снять отметку, не сохранять.

    Прошедший тест без признака в CI (повтор, временный сбой, отметка) не сохраняется: если в
    CI лежал стенд, а теперь поднялся, тест пройдёт, но нестабильным не станет.
    """
    signals = {int(test["test_result_id"]): test.get("signals") or [] for test in plan["tests"]}
    attempts = int(result.get("attempts") or 0)
    mark, unmark = [], []
    for item in result.get("tests") or []:
        test_id = int(item["test_result_id"])
        if item.get("outcome") == "passed" and not item.get("changed") and signals.get(test_id):
            mark.append({**item, "signals": signals[test_id]})
        elif (item.get("outcome") == "reproduced" and not item.get("cut")
              and int(item.get("runs") or 0) >= attempts and test_id in marks):
            unmark.append(item)
    return RememberPlan(mark, unmark)
