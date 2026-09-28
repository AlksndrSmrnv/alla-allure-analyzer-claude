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
_TIMEOUT_LINE_RE = re.compile(r"timeout|wait|delay|poll|interval|ttl|atMost|sleep", re.IGNORECASE)
_LITERAL_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\b\d+(?:\.\d+)?\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


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
    if _timeout_numbers(after) > _timeout_numbers(before):
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


def _timeout_numbers(lines: list[str]) -> float:
    total = 0.0
    for line in _strip_comments("\n".join(lines)).splitlines():
        if _TIMEOUT_LINE_RE.search(line):
            total += sum(float(number) for number in _NUMBER_RE.findall(line))
    return total


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
    """Одно место правки: БЫЛО ровно в одном месте, у указанной строки.

    БЫЛО внутри уже стоящего СТАЛО (``click()`` → ``waitUntilReady(); click()``)
    — часть готовой правки, а не новое место. Если БЫЛО нет, но СТАЛО стоит
    рядом с указанной строкой — правку уже сделали.
    """
    after_positions = find_block(lines, proposal.after)
    covered = {
        start + offset
        for start in after_positions
        for offset in find_block(proposal.after, proposal.before)
    }
    pending = _nearest(
        [p for p in find_block(lines, proposal.before) if p not in covered], proposal.line
    )
    if len(pending) == 1:
        return _Location("pending", pending[0])
    if pending:
        return _Location("ambiguous")
    if _nearest(after_positions, proposal.line):
        return _Location("applied")
    return _Location("missing")


def _nearest(positions: list[int], line: int | None) -> list[int]:
    """Кандидаты у указанной строки (±LINE_WINDOW): только самые близкие."""
    if line is None:
        return positions
    inside = [p for p in positions if abs(p + 1 - line) <= LINE_WINDOW]
    if not inside:
        return []
    best = min(abs(p + 1 - line) for p in inside)
    return [p for p in inside if abs(p + 1 - line) == best]


def _resolve(proposal: Proposal, project_root: Path, errors: list[str]) -> Path | None:
    if not proposal.file:
        errors.append("нет «ФАЙЛ:» — укажи путь от корня проекта и строку: path/Test.java:42")
        return None
    root = project_root.resolve()
    candidate = Path(proposal.file)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if root not in target.parents or not target.is_file():
        errors.append(f"файл «{proposal.file}» не найден в проекте {root}")
        return None
    parts = target.relative_to(root).parts
    if (
        target.suffix.lower() not in SOURCE_EXTENSIONS
        or any(part.startswith(".") or part in DENIED_DIRS for part in parts)
    ):
        errors.append(
            f"файл «{proposal.file}» не относится к коду автотестов (правятся только исходники "
            "тестов, не настройки, сборка и CI)"
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
        _write_bytes(files.record, (json.dumps({
            "proposal": _proposal_hash(proposal),
            "file": proposal.file,
            "line": start + 1,
            "sha_before": hashlib.sha256(original).hexdigest(),
            "sha_after": hashlib.sha256(updated).hexdigest(),
            "backup": files.backup.name,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return ApplyResult("applied", f"Правка применена: {proposal.file}\n{diff}", changed=True)


def revert_proposal(
    proposal: Proposal,
    project_root: Path,
    files: ProposalFiles,
) -> ApplyResult:
    """Вернуть файл из ``NN.orig``, если после ``apply`` его больше не меняли."""
    data = _load_record(files.record)
    if data is None:
        return ApplyResult("error", "Эта правка не применялась командой apply — откатывать нечего.")
    target = _resolve(proposal, project_root, [])
    if target is None:
        return ApplyResult("error", f"Файл «{proposal.file}» не найден — откат невозможен.")
    if hashlib.sha256(target.read_bytes()).hexdigest() != data.get("sha_after"):
        return ApplyResult("error", (
            f"{proposal.file} изменён после apply — откат затёр бы чужие правки. Верни файл "
            f"вручную (версия до правки: {files.backup})."
        ))
    if not files.backup.is_file():
        return ApplyResult("error", f"Нет резервной копии {files.backup} — откат невозможен.")
    _write_bytes(target, files.backup.read_bytes(), mode_from=target)
    files.record.unlink(missing_ok=True)
    return ApplyResult("reverted", f"Файл {proposal.file} возвращён к версии до правки.")


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
    """Отметка относится к этому предложению, а файл — ровно как после apply.

    По содержимому БЫЛО/СТАЛО нельзя отличить «уже применено» от «такой же
    фрагмент есть рядом» (три ``click()`` подряд, правка убирает один):
    повторный apply удалил бы ещё строку. Хэш файла после записи однозначен;
    любое изменение файла (откат, ручная правка) отметку обнуляет.
    """
    data = _load_record(record)
    if data is None or data.get("proposal") != _proposal_hash(proposal):
        return False
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest() == data.get("sha_after")
    except OSError:
        return False


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
