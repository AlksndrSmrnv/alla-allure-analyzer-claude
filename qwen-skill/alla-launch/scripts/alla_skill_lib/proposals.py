"""Предложения правок автотестов: ``proposals/NN.md`` и команда ``apply``.

Для кластера с категорией «тест» модель решает, есть ли конкретный дефект
в коде автотеста, и описывает правку блоками БЫЛО/СТАЛО. Скрипт проверяет,
что БЫЛО действительно есть в файле, а правка не ослабляет тест (не убирает
проверки, не отключает тест, не глушит исключения, не добавляет ожиданий
вслепую). Применяет правку только команда ``apply NN --yes`` — после явного
согласия пользователя; замена строго одного места, повторно не применяется.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

LINE_WINDOW = 20
CONTEXT_LINES = 5
EXCLUDED_DIRS = ("alla-reports", "alla-kb", ".qwen", ".git")

_HEADER_RE = re.compile(r"^(решение|файл|было|стало|почему)\s*:\s*(.*)$", re.IGNORECASE)
_DECOR_RE = re.compile(r"^[\s#>*_`-]+")
_FENCE_RE = re.compile(r"^\s*```")
_FILE_RE = re.compile(r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
_ASSERT_RE = re.compile(r"\b(?:assert\w*|verify\w*|expect\w*|should\w*)\b", re.IGNORECASE)
_SKIP_RE = re.compile(
    r"@Disabled|@Ignore|pytest\.mark\.skip|xfail|\.skip\(|enabled\s*=\s*false", re.IGNORECASE
)
_EMPTY_CATCH_RE = re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}|except[^\n:]*:\s*(?:#[^\n]*)?\s*pass\b")
_SLEEP_RE = re.compile(r"\bsleep\s*\(|Thread\.sleep|time\.sleep|\bdelay\s*\(", re.IGNORECASE)
_TIMEOUT_LINE_RE = re.compile(r"timeout|duration\.of|ofseconds|ofmillis|waitfor", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?://|#).*$", re.MULTILINE)


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


def parse_proposal(text: str) -> Proposal:
    buckets: dict[str, list[str]] = {"решение": [], "файл": [], "было": [], "стало": [], "почему": []}
    current: str | None = None
    for raw in text.splitlines():
        cleaned = _DECOR_RE.sub("", raw.replace("**", "")).strip()
        header = _HEADER_RE.match(cleaned)
        if header:
            current = header.group(1).lower()
            # Текст на строке заголовка; у БЫЛО/СТАЛО берётся из исходной строки,
            # а не из очищенной (там убраны ** и `, которые бывают в коде).
            inline = raw.split(":", 1)[1] if current in ("было", "стало") else header.group(2)
            if inline.strip():
                buckets[current].append(inline.strip())
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
    file_match = _FILE_RE.search(" ".join(buckets["файл"]))
    return Proposal(
        decision=decision,
        file=file_match.group("path").replace("\\", "/") if file_match else None,
        line=int(file_match.group("line")) if file_match and file_match.group("line") else None,
        before=_trim(buckets["было"]),
        after=_trim(buckets["стало"]),
        why=" ".join(line.strip() for line in buckets["почему"] if line.strip()),
    )


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

    lines = _read_lines(target)
    positions = find_block(lines, proposal.before)
    near = [p for p in positions if proposal.line is None or abs(p + 1 - proposal.line) <= LINE_WINDOW]
    if not near:
        errors.append(
            f"строки «БЫЛО:» не найдены в {proposal.file}"
            + (f" рядом со строкой {proposal.line}" if proposal.line else "")
            + " — скопируй их из файла дословно, с отступами. Фрагмент файла:\n"
            + _excerpt(lines, proposal.line)
        )
    errors.extend(weakening_errors(proposal.before, proposal.after))
    return errors


def weakening_errors(before: list[str], after: list[str]) -> list[str]:
    """Признаки того, что правка ослабляет тест, а не чинит его."""
    old, new = "\n".join(before), "\n".join(after)
    errors: list[str] = []
    # Закомментированная проверка — тоже удалённая проверка.
    if len(_ASSERT_RE.findall(_strip_comments(new))) < len(_ASSERT_RE.findall(_strip_comments(old))):
        errors.append("правка убирает проверки (assert/verify/expect/should) — так нельзя")
    if len(_SKIP_RE.findall(new)) > len(_SKIP_RE.findall(old)):
        errors.append("правка отключает тест — так нельзя")
    if len(_EMPTY_CATCH_RE.findall(new)) > len(_EMPTY_CATCH_RE.findall(old)):
        errors.append("правка глушит исключение пустым catch/except — так нельзя")
    if len(_SLEEP_RE.findall(new)) > len(_SLEEP_RE.findall(old)):
        errors.append("правка добавляет sleep — используй явное ожидание условия")
    if _max_timeout(after) > _max_timeout(before):
        errors.append("правка увеличивает таймаут — это не исправление дефекта теста")
    return errors


def find_block(lines: list[str], block: list[str]) -> list[int]:
    """Индексы строк, с которых начинается блок (сравнение без хвостовых пробелов)."""
    wanted = [line.rstrip() for line in block]
    size = len(wanted)
    stripped = [line.rstrip() for line in lines]
    return [i for i in range(len(stripped) - size + 1) if stripped[i:i + size] == wanted]


def apply_proposal(proposal: Proposal, project_root: Path, *, confirm: bool) -> tuple[str, str]:
    """(статус, текст): ``diff`` — показать правку, ``applied``/``error`` — итог."""
    errors = validate_proposal(proposal, project_root)
    if not proposal.is_fix:
        return "error", "Это предложение — «не трогать», применять нечего."
    target = _resolve(proposal, project_root, [])
    if target is None or errors and not _already_applied(proposal, target):
        return "error", "Правка не проходит проверку:\n" + "\n".join(f"- {e}" for e in errors)

    # read_text переводит \r\n в \n — стиль переводов строк смотрим по байтам.
    newline = "\r\n" if b"\r\n" in target.read_bytes() else "\n"
    lines = _read_lines(target)
    if _already_applied(proposal, target):
        return "applied", f"Правка уже применена: {proposal.file}"
    positions = find_block(lines, proposal.before)
    if len(positions) != 1:
        return "error", (
            f"«БЫЛО:» встречается в {proposal.file} {len(positions)} раз — "
            "код изменился или фрагмент неоднозначен, правка не применена."
        )
    start = positions[0]
    updated = lines[:start] + proposal.after + lines[start + len(proposal.before):]
    diff = "".join(difflib.unified_diff(
        [line + "\n" for line in lines],
        [line + "\n" for line in updated],
        fromfile=f"a/{proposal.file}",
        tofile=f"b/{proposal.file}",
        n=2,
    ))
    if not confirm:
        return "diff", diff
    # _read_lines сохраняет пустой последний элемент, если файл кончался
    # переводом строки, поэтому join восстанавливает его без добавок.
    target.write_text(newline.join(updated), encoding="utf-8", newline="")
    return "applied", f"Правка применена: {proposal.file}\n{diff}"


def is_applied(proposal: Proposal, project_root: Path) -> bool:
    """Правка уже в файле: СТАЛО есть, БЫЛО нет."""
    if not proposal.is_fix or not proposal.after:
        return False
    target = _resolve(proposal, project_root, [])
    return target is not None and _already_applied(proposal, target)


def _already_applied(proposal: Proposal, target: Path) -> bool:
    lines = _read_lines(target)
    return bool(find_block(lines, proposal.after)) and not find_block(lines, proposal.before)


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
    if any(part in EXCLUDED_DIRS for part in target.relative_to(root).parts):
        errors.append(f"файл «{proposal.file}» не относится к коду автотестов")
        return None
    return target


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _excerpt(lines: list[str], line: int | None) -> str:
    center = (line or 1) - 1
    start = max(0, center - CONTEXT_LINES)
    end = min(len(lines), center + CONTEXT_LINES + 1)
    return "\n".join(f"{number + 1:>5}: {lines[number]}" for number in range(start, end))


def _strip_comments(code: str) -> str:
    code = _BLOCK_COMMENT_RE.sub(" ", code)
    return _LINE_COMMENT_RE.sub("", code)


def _max_timeout(lines: list[str]) -> float:
    values = [
        float(number)
        for line in lines if _TIMEOUT_LINE_RE.search(line)
        for number in _NUMBER_RE.findall(line)
    ]
    return max(values, default=0.0)


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines
