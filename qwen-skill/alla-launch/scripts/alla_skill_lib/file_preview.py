"""Read-only previews of project configuration and Qwen files."""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

JOURNAL = ".alla-qwen-files-transaction.json"

def check_journal(root: Path) -> None:
    journal = root / JOURNAL
    if journal.exists() or journal.is_symlink():
        raise ValueError(
            f"Остался журнал прежней установки: {journal}. Инженеру нужно проверить "
            "конфигурацию и файлы вручную; автоматического восстановления нет."
        )


def safe_target(root: Path, target: Path) -> None:
    """Do not follow links in configuration paths, including the final file."""
    relative = target.relative_to(root)
    if ".." in relative.parts or not relative.parts:
        raise ValueError(f"Недопустимый путь: {target}")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Символическая ссылка в пути: {current}")
    if target.exists() and not target.is_file():
        raise ValueError(f"Ожидался обычный файл: {target}")

def read_bytes(root: Path, target: Path) -> bytes | None:
    safe_target(root, target)
    return target.read_bytes() if target.exists() else None


@dataclass(frozen=True)
class Change:
    target: Path
    before: bytes | None
    after: bytes | None

def change(root: Path, target: Path, after: bytes | None) -> Change:
    return Change(target, read_bytes(root, target), after)

def preview(root: Path, changes: Sequence[Change]) -> str:
    lines = []
    for item in changes:
        if item.before == item.after:
            continue
        name = str(item.target.relative_to(root))
        try:
            old = (item.before or b"").decode("utf-8")
            new = (item.after or b"").decode("utf-8")
        except UnicodeDecodeError:
            lines.append(f"Binary {name}: " + " -> ".join(
                "absent" if data is None else
                f"{len(data)} bytes sha256={hashlib.sha256(data).hexdigest()}"
                for data in (item.before, item.after)))
            continue
        lines.extend(difflib.unified_diff(old.splitlines(keepends=True),
                                         new.splitlines(keepends=True),
                                         fromfile="/dev/null" if item.before is None else "a/" + name,
                                         tofile="/dev/null" if item.after is None else "b/" + name))
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def file_instructions(root: Path, files: Sequence[tuple[Path, Path]]) -> str:
    """Print complete ready files and the diff against existing targets."""
    lines = []
    changes = []
    for source, target in files:
        text = source.read_text(encoding="utf-8")
        data = (text if text.endswith("\n") else text + "\n").encode()
        item = change(root, target, data)
        if item.before == item.after:
            continue
        lines.append(f"Источник: {source}\nНазначение: {target}\nГотовое содержимое:\n{data.decode()}")
        changes.append(item)
    if changes:
        lines.append("Diff:\n" + preview(root, changes))
    return "\n".join(lines)
