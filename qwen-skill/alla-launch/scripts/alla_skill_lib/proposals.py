"""Предложения правок автотестов: ``proposals/NN.md``, ``apply`` и ``revert``.

Для кластера с категорией «тест» модель решает, есть ли конкретный дефект
в коде автотеста, и описывает правку блоками БЫЛО/СТАЛО. Скрипт проверяет,
что БЫЛО действительно есть в файле, а правка явно не ослабляет тест (не
убирает проверки, не отключает тест, не глушит исключения, не добавляет
sleep). Менее очевидное — смена ожидаемого значения, рост таймаута — не
отказ, а предупреждение рядом с diff: решает человек.

Правку применяет только ``apply NN --yes --diff <хэш>``: хэш есть у diff,
который скрипт только что показал, поэтому применяется ровно увиденное.
Перед записью файл сохраняется в ``NN.orig``, запись атомарна, а
``revert NN`` возвращает файл, если после ``apply`` его не меняли.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from alla_skill_lib.code_hints import SOURCE_EXTENSIONS

LINE_WINDOW = 20
CONTEXT_LINES = 5
ANCHOR_LINES = 3  # строк вокруг правки, которые apply записывает в отметку применения
# Не код автотестов: служебные папки скилла, зависимости и результаты сборки
# отчётов. Всё с точки в имени (.git, .github, .env) закрыто отдельно.
DENIED_DIRS = frozenset({
    "alla-reports", "alla-kb", "node_modules", "__pycache__", "allure-results", "allure-report",
})

_HEADER_RE = re.compile(r"^(решение|файл|было|стало|почему)\s*:\s*(.*)$", re.IGNORECASE)
_DECOR_RE = re.compile(r"^[\s#>*_`-]+")
_INLINE_CODE_RE = re.compile(
    r"^[\s#>*_`\-]*(?:было|стало)\s*[*_`]*\s*:\s*[*_`]*[ \t]?(.*)$", re.IGNORECASE
)
_FENCE_RE = re.compile(r"^\s*```")
_FILE_LINE_RE = re.compile(
    r"^(?P<path>.+?\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?(?:\s+[—–-]\s.*|\s*\(.*\))?\s*$"
)
_FILE_TOKEN_RE = re.compile(r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
# Проверка — это вызов или оператор, а не любое слово «expected»: переименование
# аргумента assertEquals(expectedCode, …) → assertEquals(201, …) проверок не убирает.
_ASSERT_RE = re.compile(
    r"\b(?:assert\w*|verify\w*|expect\w*|should\w*)\s*\("
    r"|(?m:^\s*assert\b)"
    r"|\.statusCode\s*\("
    r"|(?m:^\s*Then\b)",
    re.IGNORECASE,
)
_SKIP_RE = re.compile(
    r"@Disabled|@Ignore|\[Ignore\]|Skip\s*=|pytest\.mark\.skip|xfail|\.skip\(|\bxit\(|\bxdescribe\("
    r"|enabled\s*=\s*false|@Retry\w*|@RepeatedTest|@Flaky",
    re.IGNORECASE,
)
_EMPTY_CATCH_RE = re.compile(
    r"catch\s*(?:\([^)]*\))?\s*\{\s*\}"
    r"|except[^\n:]*:\s*(?:#[^\n]*)?\s*pass\b"
    r"|\.catch\(\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*(?:\{\s*\}|null|undefined|void 0)\s*\)"
)
_SLEEP_RE = re.compile(r"\bsleep\s*\(|Thread\.sleep|time\.sleep|\bdelay\s*\(", re.IGNORECASE)
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT_MS = {
    "ms": 1, "milli": 1, "millis": 1, "millisecond": 1, "milliseconds": 1,
    "s": 1000, "sec": 1000, "secs": 1000, "second": 1000, "seconds": 1000,
    "min": 60_000, "mins": 60_000, "minute": 60_000, "minutes": 60_000,
    "h": 3_600_000, "hour": 3_600_000, "hours": 3_600_000,
}
# Длительности с единицами: (regex, номер группы числа, номер группы единицы).
_DURATION_PATTERNS: tuple[tuple[re.Pattern[str], int, int], ...] = (
    # Duration.ofSeconds(30), ofMillis(500)
    (re.compile(rf"\bof(millis|seconds|minutes|hours)\s*\(\s*{_NUM}", re.IGNORECASE), 2, 1),
    # 60, TimeUnit.SECONDS / (5, ChronoUnit.MINUTES)
    (re.compile(
        rf"{_NUM}\s*,\s*(?:TimeUnit\.|ChronoUnit\.)?(milliseconds|millis|seconds|minutes|hours)\b",
        re.IGNORECASE,
    ), 1, 2),
    # timedelta(seconds=30)
    (re.compile(rf"\b(milliseconds|seconds|minutes|hours)\s*=\s*{_NUM}", re.IGNORECASE), 2, 1),
    # 30s, 500ms, 2min, 30.seconds
    (re.compile(
        rf"\b{_NUM}\s*\.?\s*(ms|millis(?:econds?)?|secs?|seconds?|s|mins?|minutes?|hours?|h)\b",
        re.IGNORECASE,
    ), 1, 2),
)
# Число без единиц засчитывается, только если стоит прямо у ключа ожидания:
# timeout=5, setTimeout(60000), pollInterval: 200.
_RAW_TIMEOUT_RE = re.compile(
    rf"\b(\w*(?:timeout|wait|delay|poll|interval|ttl)\w*)[\"']?\s*[=:(]\s*{_NUM}", re.IGNORECASE
)
_NAME_BEFORE_RE = re.compile(r"([A-Za-z_]\w*)\s*$")
_ASSIGNED_KEY_RE = re.compile(r"([A-Za-z_]\w*)[\"']?\s*[=:]\s*$")
_LITERAL_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\b\d+(?:\.\d+)?\b")


class SourceEncodingError(Exception):
    """Файл не в UTF-8: скилл не берётся его править."""


@dataclass
class Proposal:
    decision: str  # fix | skip | ?
    file: str | None
    line: int | None
    before: list[str]
    after: list[str]
    why: str

    @property
    def is_fix(self) -> bool:
        return self.decision == "fix"


@dataclass(frozen=True)
class ProposalFiles:
    """Служебные файлы предложения в папке разбора."""

    record: Path  # NN.applied.json: что и когда применено
    backup: Path  # NN.orig: файл до правки
    patch: Path  # NN.patch: показанный diff


@dataclass(frozen=True)
class ApplyResult:
    """``diff`` — показан diff (``diff_hash`` нужен для ``--yes``), ``applied``/``error``."""

    status: str
    text: str
    diff_hash: str | None = None
    changed: bool = False  # файл записан именно этим вызовом


# ---------------------------------------------------------------------------
# Разбор и проверка предложения
# ---------------------------------------------------------------------------


def parse_proposal(text: str) -> Proposal:
    buckets: dict[str, list[str]] = {"решение": [], "файл": [], "было": [], "стало": [], "почему": []}
    current: str | None = None
    for raw in text.lstrip("﻿").splitlines():
        cleaned = _DECOR_RE.sub("", raw.replace("**", "")).strip()
        header = _HEADER_RE.match(cleaned)
        if header:
            current = header.group(1).lower()
            if current in ("было", "стало"):
                # Код со строки заголовка берётся из исходной строки: в очищенной
                # убраны ``**`` и «`», которые бывают в самом коде.
                inline = _INLINE_CODE_RE.match(raw)
                text_after = _strip_inline_ticks(inline.group(1)) if inline else ""
                if text_after.strip():
                    buckets[current].append(text_after.rstrip())
            elif header.group(2).strip():
                buckets[current].append(header.group(2).strip())
            continue
        if current is not None and not _FENCE_RE.match(raw):
            buckets[current].append(raw.rstrip())

    decision_text = " ".join(buckets["решение"]).lower()
    if "не трог" in decision_text or "не исправ" in decision_text or decision_text.startswith("нет"):
        decision = "skip"
    elif "исправ" in decision_text:
        decision = "fix"
    else:
        decision = "?"
    file_path, line = _parse_file(" ".join(buckets["файл"]))
    return Proposal(
        decision=decision,
        file=file_path,
        line=line,
        before=_trim(buckets["было"]),
        after=_trim(buckets["стало"]),
        why=" ".join(line.strip() for line in buckets["почему"] if line.strip()),
    )


def _strip_inline_ticks(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped.startswith("`") and stripped.endswith("`"):
        return stripped.strip("`")
    return text


def _parse_file(text: str) -> tuple[str | None, int | None]:
    """Путь и строка из «ФАЙЛ:»; пробелы в пути допустимы: ``My Tests/A.java:6``."""
    text = text.replace("`", "").strip().strip("\"'").strip()
    match = _FILE_LINE_RE.match(text) or _FILE_TOKEN_RE.search(text)
    if not match:
        return None, None
    line = match.group("line")
    return match.group("path").strip().strip("`\"'").replace("\\", "/"), int(line) if line else None


def validate_proposal(proposal: Proposal, project_root: Path) -> list[str]:
    """Список проблем предложения (пусто — принято)."""
    if proposal.decision == "?":
        return ["«РЕШЕНИЕ:» должно быть «исправить» или «не трогать»"]
    if not proposal.why:
        return ["нет «ПОЧЕМУ:» — объясни решение фактами из данных и кода"]
    if not proposal.is_fix:
        return []

    errors: list[str] = []
    target = _resolve(proposal, project_root, errors)
    if target is None:
        return errors
    if not proposal.before:
        errors.append("нет «БЫЛО:» — скопируй строки из файла дословно")
    if not proposal.after:
        errors.append("нет «СТАЛО:» — напиши строки после правки")
    if errors:
        return errors
    if [line.rstrip() for line in proposal.before] == [line.rstrip() for line in proposal.after]:
        return ["«СТАЛО:» совпадает с «БЫЛО:» — правки нет"]

    try:
        text = _read_source(target)
    except SourceEncodingError as exc:
        return [str(exc)]
    lines = _normalize(text).split("\n")
    location = _locate(lines, proposal)
    where = f" рядом со строкой {proposal.line}" if proposal.line else ""
    if location.state == "missing":
        errors.append(
            f"строки «БЫЛО:» не найдены в {proposal.file}{where} — скопируй их из файла "
            "дословно, с отступами. Фрагмент файла:\n" + _excerpt(lines, proposal.line)
        )
    elif location.state == "ambiguous":
        errors.append(
            f"в {proposal.file}{where} несколько одинаковых мест для «БЫЛО:» — добавь в БЫЛО "
            "и СТАЛО соседнюю строку, чтобы место было однозначным. Фрагмент файла:\n"
            + _excerpt(lines, proposal.line)
        )
    errors.extend(weakening_errors(proposal.before, proposal.after))
    return errors


def weakening_errors(before: list[str], after: list[str]) -> list[str]:
    """Явные признаки того, что правка ослабляет тест, а не чинит его."""
    old, new = "\n".join(before), "\n".join(after)
    errors: list[str] = []
    # Закомментированная проверка — тоже удалённая проверка.
    if len(_ASSERT_RE.findall(_strip_comments(new))) < len(_ASSERT_RE.findall(_strip_comments(old))):
        errors.append("правка убирает проверки (assert/verify/expect/should) — так нельзя")
    if len(_SKIP_RE.findall(new)) > len(_SKIP_RE.findall(old)):
        errors.append("правка отключает тест или добавляет повторы вместо исправления — так нельзя")
    if len(_EMPTY_CATCH_RE.findall(new)) > len(_EMPTY_CATCH_RE.findall(old)):
        errors.append("правка глушит исключение пустым catch/except — так нельзя")
    if len(_SLEEP_RE.findall(new)) > len(_SLEEP_RE.findall(old)):
        errors.append("правка добавляет sleep — используй явное ожидание условия")
    return errors


def weakening_warnings(before: list[str], after: list[str]) -> list[str]:
    """Что человеку стоит проверить в diff: это не отказ, а повод посмотреть внимательнее."""
    warnings: list[str] = []
    expected = _assertion_literals(before), _assertion_literals(after)
    removed, added = expected[0] - expected[1], expected[1] - expected[0]
    if removed and added:
        warnings.append(
            "меняется ожидаемое значение в проверке ("
            + ", ".join(sorted(removed.elements())) + " → " + ", ".join(sorted(added.elements()))
            + "): убедись, что новое поведение приложения задумано, а не регрессия"
        )
    if _timeouts_increased(before, after):
        warnings.append(
            "увеличено значение ожидания или таймаута: это лечит симптом, если тест ждёт "
            "не то условие"
        )
    return warnings


def _assertion_literals(lines: list[str]) -> Counter[str]:
    found: Counter[str] = Counter()
    for line in _strip_comments("\n".join(lines)).splitlines():
        if _ASSERT_RE.search(line):
            found.update(_LITERAL_RE.findall(line))
    return found


def _durations(lines: list[str]) -> list[tuple[str, float]]:
    """Ожидания в порядке появления: (ключ, значение).

    Ключ — группа единиц (``ms`` — пересчитано в миллисекунды, ``raw`` — число
    без единиц) и «чьё это ожидание»: вызов, внутри которого стоит значение
    (``a(…)``, ``withTimeout(…)``, ``timedelta(…)``), или ключ присваивания
    (``timeout: 30s``). По ключу перестановка целых вызовов отличается от
    переноса значения из одного вызова в другой.
    """
    found: list[tuple[int, int, str, float]] = []  # (строка, позиция, ключ, значение)
    for number, line in enumerate(lines):
        rest = line
        for pattern, number_group, unit_group in _DURATION_PATTERNS:
            for match in pattern.finditer(rest):
                unit = match.group(unit_group).lower()
                value = float(match.group(number_group)) * _UNIT_MS.get(unit, 1)
                key = "ms:" + _owner(line[:match.start()])
                found.append((number, match.start(), key, value))
            # Пробелы той же длины: позиции сохраняются, а число не засчитается
            # ещё и «без единиц».
            rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
        for match in _RAW_TIMEOUT_RE.finditer(rest):
            key = f"raw:{_owner(line[:match.start()])}.{match.group(1).lower()}"
            found.append((number, match.start(), key, float(match.group(2))))
    return [(key, value) for _, _, key, value in sorted(found)]


def _owner(prefix: str) -> str:
    """Имя вызова, в скобках которого стоит значение, или ключ присваивания."""
    depth = 0
    for index in range(len(prefix) - 1, -1, -1):
        char = prefix[index]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                name = _NAME_BEFORE_RE.search(prefix[:index])
                return name.group(1).lower() if name else ""
            depth -= 1
    assigned = _ASSIGNED_KEY_RE.search(prefix)
    return assigned.group(1).lower() if assigned else ""


def _timeouts_increased(before: list[str], after: list[str]) -> bool:
    """Какое-то ожидание заменено большим (с учётом единиц).

    Ожидания сопоставляются по одному внутри своего ключа (см.
    :func:`_durations`), по порядку появления: уменьшение одного не скрывает
    увеличение другого (60→30 с и 1→5 с), перенос значения из ``a`` в ``b``
    виден, а перестановка целых вызовов — не увеличение. Значения, у которых
    нет пары по ключу (вызов переименован, добавлен или убран), сравниваются
    между собой по возрастанию. Новое ожидание без замены старого (явное
    ожидание условия вместо его отсутствия) — не увеличение.
    """
    old_by_key: dict[str, list[float]] = {}
    new_by_key: dict[str, list[float]] = {}
    for key, value in _durations(before):
        old_by_key.setdefault(key, []).append(value)
    for key, value in _durations(after):
        new_by_key.setdefault(key, []).append(value)

    unpaired_old: dict[str, list[float]] = {}
    unpaired_new: dict[str, list[float]] = {}
    for key in old_by_key.keys() | new_by_key.keys():
        old, new = old_by_key.get(key, []), new_by_key.get(key, [])
        if any(new_value > old_value for old_value, new_value in zip(old, new)):
            return True
        group = key.split(":", 1)[0]
        unpaired_old.setdefault(group, []).extend(old[len(new):])
        unpaired_new.setdefault(group, []).extend(new[len(old):])

    for group, old in unpaired_old.items():
        removed = sorted((Counter(old) - Counter(unpaired_new.get(group, []))).elements())
        added = sorted((Counter(unpaired_new.get(group, [])) - Counter(old)).elements())
        if any(new_value > old_value for old_value, new_value in zip(removed, added)):
            return True
    return False


# ---------------------------------------------------------------------------
# Место правки
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Location:
    """Место правки: pending — ещё БЫЛО, applied — уже СТАЛО, missing/ambiguous."""

    state: str
    position: int | None = None


def find_block(lines: list[str], block: list[str]) -> list[int]:
    """Индексы строк, с которых начинается блок (сравнение без хвостовых пробелов)."""
    wanted = [line.rstrip() for line in block]
    size = len(wanted)
    stripped = [line.rstrip() for line in lines]
    return [i for i in range(len(stripped) - size + 1) if stripped[i:i + size] == wanted]


def _locate(lines: list[str], proposal: Proposal) -> _Location:
    """Одно место правки — общее для проверки, замены и отметки «применено».

    Кандидаты — ещё не исправленные вхождения БЫЛО и уже вставленные блоки
    СТАЛО; берётся ближайший к указанной строке (в пределах ±LINE_WINDOW).
    Если он — СТАЛО, правка здесь уже стоит, и соседнее БЫЛО не трогается.

    БЫЛО внутри вставленного СТАЛО (``click()`` → ``waitUntilReady(); click()``)
    — часть готовой правки, а не новое место; СТАЛО внутри ещё целого БЫЛО
    (``click(); click()`` → ``click()``) — исходный код, а не признак применения.
    """
    after_positions = find_block(lines, proposal.after)
    covered = {
        start + offset
        for start in after_positions
        for offset in find_block(proposal.after, proposal.before)
    }
    pending = [p for p in find_block(lines, proposal.before) if p not in covered]
    inside_pending = {
        start + offset
        for start in pending
        for offset in find_block(proposal.before, proposal.after)
    }
    candidates = [(p, "pending") for p in pending]
    candidates += [(p, "applied") for p in after_positions if p not in inside_pending]
    if proposal.line is not None:
        line = proposal.line
        candidates = [c for c in candidates if abs(c[0] + 1 - line) <= LINE_WINDOW]
        if candidates:
            best = min(abs(position + 1 - line) for position, _ in candidates)
            candidates = [c for c in candidates if abs(c[0] + 1 - line) == best]
    if not candidates:
        return _Location("missing")
    states = {state for _, state in candidates}
    if states == {"applied"}:
        return _Location("applied", candidates[0][0])
    if states == {"pending"} and len(candidates) == 1:
        return _Location("pending", candidates[0][0])
    return _Location("ambiguous")


def _resolve(proposal: Proposal, project_root: Path, errors: list[str]) -> Path | None:
    return _resolve_file(proposal.file, project_root, errors)


def _resolve_file(file: str | None, project_root: Path, errors: list[str]) -> Path | None:
    if not file:
        errors.append("нет «ФАЙЛ:» — укажи путь от корня проекта и строку: path/Test.java:42")
        return None
    root = project_root.resolve()
    candidate = Path(file)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if root not in target.parents or not target.is_file():
        errors.append(f"файл «{file}» не найден в проекте {root}")
        return None
    parts = target.relative_to(root).parts
    if (
        target.suffix.lower() not in SOURCE_EXTENSIONS
        or any(part.startswith(".") or part in DENIED_DIRS for part in parts)
    ):
        errors.append(
            f"файл «{file}» не относится к коду автотестов (правятся только исходники "
            "тестов, не настройки, сборку и CI)"
        )
        return None
    return target


# ---------------------------------------------------------------------------
# apply и revert
# ---------------------------------------------------------------------------


def apply_proposal(
    proposal: Proposal,
    project_root: Path,
    *,
    confirm: bool = False,
    diff_hash: str | None = None,
    files: ProposalFiles | None = None,
) -> ApplyResult:
    """Показать diff или, с ``confirm`` и хэшем показанного diff, применить правку."""
    if not proposal.is_fix:
        return ApplyResult("error", "Это предложение — «не трогать», применять нечего.")
    target = _resolve(proposal, project_root, [])
    if target is not None and files is not None and _recorded(proposal, target, files.record):
        return ApplyResult("applied", (
            f"Правка уже применена: {proposal.file} (отметка {files.record}; чтобы вернуть файл — "
            "команда revert)."
        ))
    errors = validate_proposal(proposal, project_root)
    if target is None or errors:
        return ApplyResult(
            "error", "Правка не проходит проверку:\n" + "\n".join(f"- {e}" for e in errors)
        )

    original = target.read_bytes()
    text = _read_source(target)
    lines = _normalize(text).split("\n")
    location = _locate(lines, proposal)
    if location.state == "applied":
        return ApplyResult("applied", f"Правка уже применена: {proposal.file}")
    if location.state != "pending" or location.position is None:
        return ApplyResult("error", "Место правки не определено однозначно — правка не применена.")
    start = location.position
    updated_text = _splice(text, start, len(proposal.before), proposal.after)
    updated_lines = _normalize(updated_text).split("\n")
    diff = "".join(difflib.unified_diff(
        [line + "\n" for line in lines],
        [line + "\n" for line in updated_lines],
        fromfile=f"a/{proposal.file}",
        tofile=f"b/{proposal.file}",
        n=2,
    ))
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:8]

    if not (confirm and diff_hash == digest):
        if files is not None:
            _write_bytes(files.patch, diff.encode("utf-8"))
        prefix = ""
        if confirm:
            prefix = (
                "Хэш --diff не совпал с текущим diff (или не указан): файл или предложение "
                "изменились с момента показа. Покажи пользователю diff ниже заново.\n"
            )
        warnings = weakening_warnings(proposal.before, proposal.after)
        notes = "".join(f"\nПроверь: {warning}" for warning in warnings)
        return ApplyResult("diff", prefix + diff + notes, digest)

    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8"
    if files is not None:
        _write_bytes(files.backup, original)
    updated = updated_text.encode(encoding)
    _write_bytes(target, updated, mode_from=target)
    if files is not None:
        end = start + len(proposal.after)
        _write_bytes(files.record, (json.dumps({
            "proposal": _proposal_hash(proposal),
            "file": proposal.file,
            "line": start + 1,
            "sha_before": hashlib.sha256(original).hexdigest(),
            "sha_after": hashlib.sha256(updated).hexdigest(),
            "backup": files.backup.name,
            # Строки вокруг СТАЛО: по ним потом узнаётся именно это место.
            "context_before": [line.rstrip() for line in updated_lines[max(0, start - ANCHOR_LINES):start]],
            "context_after": [line.rstrip() for line in updated_lines[end:end + ANCHOR_LINES]],
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return ApplyResult("applied", f"Правка применена: {proposal.file}\n{diff}", changed=True)


def revert_proposal(project_root: Path, files: ProposalFiles) -> ApplyResult:
    """Вернуть файл из ``NN.orig``, если после ``apply`` его больше не меняли.

    Файл берётся из записи применения, а не из предложения: предложение можно
    переписать (другой ``ФАЙЛ:``), и откат ушёл бы не в тот файл.
    """
    data = _load_record(files.record)
    if data is None:
        return ApplyResult("error", "Эта правка не применялась командой apply — откатывать нечего.")
    if not data.get("sha_after") or not data.get("file"):
        return ApplyResult("error", (
            "Отметка применения старого формата (без хэша файла и резервной копии) — откат "
            "невозможен, верни файл вручную (git)."
        ))
    errors: list[str] = []
    target = _resolve_file(str(data["file"]), project_root, errors)
    if target is None:
        return ApplyResult("error", f"Откат невозможен: {errors[0]}")
    if hashlib.sha256(target.read_bytes()).hexdigest() != data["sha_after"]:
        return ApplyResult("error", (
            f"{data['file']} изменён после apply — откат затёр бы чужие правки. Верни файл "
            f"вручную (версия до правки: {files.backup})."
        ))
    if not files.backup.is_file():
        return ApplyResult("error", f"Нет резервной копии {files.backup} — откат невозможен.")
    _write_bytes(target, files.backup.read_bytes(), mode_from=target)
    files.record.unlink(missing_ok=True)
    return ApplyResult("reverted", f"Файл {data['file']} возвращён к версии до правки.")


def is_applied(
    proposal: Proposal,
    project_root: Path,
    files: ProposalFiles | None = None,
) -> bool:
    """Правка применена: есть отметка с тем же содержимым файла или СТАЛО уже на месте."""
    if not proposal.is_fix or not proposal.after:
        return False
    target = _resolve(proposal, project_root, [])
    if target is None:
        return False
    if files is not None and _recorded(proposal, target, files.record):
        return True
    try:
        lines = _normalize(_read_source(target)).split("\n")
    except SourceEncodingError:
        return False
    return _locate(lines, proposal).state == "applied"


def _proposal_hash(proposal: Proposal) -> str:
    material = "\n".join([
        proposal.decision, str(proposal.file), str(proposal.line),
        *proposal.before, "␞", *proposal.after,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_record(record: Path) -> dict[str, object] | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recorded(proposal: Proposal, target: Path, record: Path) -> bool:
    """Отметка относится к этому предложению, и правка по-прежнему на месте.

    По содержимому БЫЛО/СТАЛО нельзя отличить «уже применено» от «такой же
    фрагмент есть рядом» (три ``click()`` подряд, правка убирает один):
    повторный apply удалил бы ещё строку. Поэтому применение записывается.

    Хэш файла после записи — быстрый путь. Если он не совпал, файл менялся:
    возможно, другой правкой, и это не значит, что наша откачена. Тогда нужно
    подтвердить именно наш участок (см. :func:`_still_in_place`): такое же
    СТАЛО в другом месте файла применения не доказывает. Файл, вернувшийся к
    версии до правки (хэш ``sha_before``), применённым не считается.
    """
    data = _load_record(record)
    if data is None or data.get("proposal") != _proposal_hash(proposal):
        return False
    try:
        current = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return False
    if current == data.get("sha_after"):
        return True
    if current == data.get("sha_before"):
        return False
    try:
        lines = _normalize(_read_source(target)).split("\n")
        return _still_in_place(proposal, data, lines)
    except (KeyError, TypeError, ValueError, SourceEncodingError):
        return False


def _still_in_place(proposal: Proposal, data: dict[str, object], lines: list[str]) -> bool:
    """СТАЛО стоит именно там, где его записал apply.

    Место узнаётся по строкам вокруг СТАЛО, записанным при apply: они
    переживают правки в других частях файла, которые сдвигают номера строк.
    Совпавших мест может быть несколько (одинаковые методы) — тогда верим,
    только если одно из них на записанной строке. Отметка старого формата
    (без окружения) подтверждает только СТАЛО ровно на записанной строке.
    """
    recorded_line = int(data["line"])  # type: ignore[call-overload]
    positions = find_block(lines, proposal.after)
    if "context_before" not in data or "context_after" not in data:
        return recorded_line - 1 in positions
    before = [str(line) for line in data["context_before"]]  # type: ignore[attr-defined]
    after = [str(line) for line in data["context_after"]]  # type: ignore[attr-defined]
    size = len(proposal.after)
    matched = [
        position for position in positions
        if position >= len(before) and position + size + len(after) <= len(lines)
        and (before or position == 0)  # пустое окружение — правка была у края файла
        and (after or position + size == len(lines))
        and [line.rstrip() for line in lines[position - len(before):position]] == before
        and [line.rstrip() for line in lines[position + size:position + size + len(after)]] == after
    ]
    return len(matched) == 1 or recorded_line - 1 in matched


# ---------------------------------------------------------------------------
# Файлы: чтение, построчные окончания, атомарная запись
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceEncodingError(
            f"файл «{path.name}» не в кодировке UTF-8 — скилл такие файлы не правит. "
            "Ответь «РЕШЕНИЕ: не трогать» и опиши правку словами в «ПОЧЕМУ:»"
        ) from exc


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _splice(text: str, start: int, count: int, after: list[str]) -> str:
    """Заменить ``count`` строк с номера ``start`` на ``after``, сохранив окончания строк.

    Оставшиеся строки не трогаются вообще (в файле может быть смесь ``\\n`` и
    ``\\r\\n``); новым строкам достаются окончания заменяемых, лишним — ближайшее
    непустое окончание блока.
    """
    parts = re.split(r"(\r\n|\r|\n)", text)
    lines, endings = parts[0::2], [*parts[1::2], ""]  # у последней строки может не быть перевода
    dominant = next((ending for ending in endings if ending), "\n")
    last_ending = endings[start + count - 1]
    fill = last_ending or dominant
    new_endings = [
        endings[start + i] if i < count - 1 and endings[start + i] else fill
        for i in range(len(after) - 1)
    ]
    new_endings.append(last_ending)
    lines[start:start + count] = after
    endings[start:start + count] = new_endings
    return "".join(line + ending for line, ending in zip(lines, endings))


def _write_bytes(path: Path, data: bytes, *, mode_from: Path | None = None) -> None:
    """Записать файл целиком через временный файл рядом (без обрыва посередине)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        if mode_from is not None:
            shutil.copymode(mode_from, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _excerpt(lines: list[str], line: int | None) -> str:
    center = (line or 1) - 1
    start = max(0, center - CONTEXT_LINES)
    end = min(len(lines), center + CONTEXT_LINES + 1)
    return "\n".join(f"{number + 1:>5}: {lines[number]}" for number in range(start, end))


def _strip_comments(code: str) -> str:
    """Код без комментариев ``//``, ``#`` и ``/* */``; в строках (``"#total"``, URL) ничего не режется."""
    out: list[str] = []
    quote = ""
    index, size = 0, len(code)
    while index < size:
        char = code[index]
        pair = code[index:index + 2]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(code[index + 1])
                index += 1
            elif char == quote or (char == "\n" and quote != "`"):
                quote = ""
        elif char in "\"'`":
            quote = char
            out.append(char)
        elif pair == "/*":
            end = code.find("*/", index + 2)
            index = size if end == -1 else end + 1
            out.append(" ")
        elif pair == "//" or char == "#":
            end = code.find("\n", index)
            index = size if end == -1 else end - 1
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines
