"""Reviewable project file changes, with snapshot checks and crash recovery."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

JOURNAL = ".alla-qwen-files-transaction.json"
LOCK = ".alla-qwen-files.lock"


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


def _encoded(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _rows(root: Path, changes: Sequence[Change]) -> list[dict[str, str | None]]:
    return [{"path": str(item.target.relative_to(root)), "before": _encoded(item.before),
             "after": _encoded(item.after)} for item in changes]


def digest(root: Path, changes: Sequence[Change]) -> str:
    payload = {"project_root": str(root.resolve()), "changes": _rows(root, changes)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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


def _write(target: Path, data: bytes | None) -> None:
    if data is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".alla-write-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def _sync(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _recover(root: Path) -> None:
    journal = root / JOURNAL
    if not journal.exists():
        return
    safe_target(root, journal)
    try:
        metadata = journal.stat()
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid() or metadata.st_nlink != 1):
            raise ValueError("журнал должен принадлежать пользователю и иметь права 600")
        record = json.loads(journal.read_text(encoding="utf-8"))
        if record["project_root"] != str(root.resolve()):
            raise ValueError("другой корень проекта")
        changes = [Change(root / row["path"],
                          None if row["before"] is None else base64.b64decode(row["before"], validate=True),
                          None if row["after"] is None else base64.b64decode(row["after"], validate=True))
                   for row in record["changes"]]
        for item in changes:
            relative = item.target.relative_to(root)
            if not (relative == Path("alla-extensions/config.toml") or
                    (relative.parent in (Path(".qwen/agents"), Path(".qwen/commands/alla"))
                     and relative.suffix == ".md" and ".." not in relative.parts)):
                raise ValueError(f"недопустимый путь в журнале: {relative}")
            if read_bytes(root, item.target) not in (item.before, item.after):
                raise ValueError(f"файл изменён извне: {item.target}")
        for item in reversed(changes):
            _write(item.target, item.before)
        for parent in {item.target.parent for item in changes}:
            if parent.is_dir():
                _sync(parent)
        _sync(root)
        journal.unlink()
        _sync(root)
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise ValueError(f"Незавершённая запись; восстановление из {journal}: {exc}") from exc


def apply(root: Path, changes: Sequence[Change], expected: str | None) -> None:
    """Apply exactly a reviewed snapshot. Journal is durable before the first write."""
    if os.name == "nt":
        raise ValueError("Транзакционная установка файлов Qwen требует Linux или macOS.")
    import fcntl

    root = root.resolve()
    safe_target(root, root / LOCK)
    fd = os.open(root / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover(root)
        if not expected or expected != digest(root, changes):
            raise ValueError("Нужен --diff ХЭШ свежего просмотра; покажи diff и спроси согласие заново.")
        for item in changes:
            if read_bytes(root, item.target) != item.before:
                raise ValueError(f"Файл изменился после просмотра: {item.target}. Покажи новый diff.")
        journal = root / JOURNAL
        safe_target(root, journal)
        _write(journal, json.dumps({"project_root": str(root), "changes": _rows(root, changes)},
                                   sort_keys=True).encode())
        _sync(root)
        try:
            for item in changes:
                safe_target(root, item.target)
                if item.before != item.after:
                    _write(item.target, item.after)
            # Sync every affected directory before dropping recovery information.
            for parent in {item.target.parent for item in changes}:
                if parent.is_dir():
                    _sync(parent)
            journal.unlink()
            _sync(root)
        except BaseException:
            # A final directory fsync can fail after unlinking the journal. Keep
            # the in-memory snapshot recoverable as well as pre-commit failures.
            if not journal.exists():
                _write(journal, json.dumps({"project_root": str(root),
                                           "changes": _rows(root, changes)}).encode())
            _recover(root)
            raise


def recover(root: Path) -> None:
    """Recover before preview, but create no files for the ordinary read-only preview."""
    if not (root / JOURNAL).exists():
        return
    if os.name == "nt":
        raise ValueError("Восстановление транзакции файлов Qwen требует Linux или macOS.")
    import fcntl

    safe_target(root, root / LOCK)
    fd = os.open(root / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover(root)
