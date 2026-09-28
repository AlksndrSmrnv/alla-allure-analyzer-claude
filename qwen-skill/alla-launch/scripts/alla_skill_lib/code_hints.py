"""Подсказки, где в проекте автотестов искать код упавшего теста.

Скилл лежит в проекте автотестов, поэтому по ``full_name`` теста и кадрам
стека можно найти исходник и строку. Это экономит агенту поиск: он сразу
открывает 1–3 нужных файла, а не обходит весь репозиторий.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath

SOURCE_EXTENSIONS = frozenset({
    ".java", ".kt", ".kts", ".groovy", ".scala",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".cs", ".go", ".rb", ".php", ".feature",
})
SKIP_DIRS = frozenset({
    ".git", ".qwen", ".idea", ".vscode", ".gradle", ".mvn", ".venv", "venv",
    "node_modules", "build", "target", "out", "dist", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "allure-results",
    "allure-report", "alla-reports",
})
MAX_INDEXED_FILES = 50_000
MAX_SCANNED_BYTES = 1_000_000

_JAVA_FRAME_RE = re.compile(r"^\s*at\s+(?:[\w.$-]+/)?([\w.$<>]+)\((\w[\w$-]*\.\w+):(\d+)\)")
_PATH_FRAME_RE = re.compile(
    r"""(?:File\s+"(?P<py>[^"]+)",\s+line\s+(?P<pyline>\d+))"""
    r"""|(?:\(?(?P<js>(?:[A-Za-z]:)?[^\s():]+\.\w+):(?P<jsline>\d+)(?::\d+)?\)?\s*$)"""
)
_PARAMS_RE = re.compile(r"\[.*\]$|\(.*\)$")


@dataclass(frozen=True)
class CodeHint:
    """Файл проекта (путь относительно корня) и, если известна, строка."""

    path: str
    line: int | None
    reason: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location} — {self.reason}"


class ProjectIndex:
    """Ленивый индекс исходников проекта по имени файла без расширения."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._by_stem: dict[str, list[PurePath]] | None = None

    def _build(self) -> dict[str, list[PurePath]]:
        index: dict[str, list[PurePath]] = {}
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                path = PurePath(filename)
                if path.suffix not in SOURCE_EXTENSIONS:
                    continue
                rel = PurePath(os.path.relpath(os.path.join(dirpath, filename), self.root))
                index.setdefault(path.stem, []).append(rel)
                count += 1
                if count >= MAX_INDEXED_FILES:
                    return index
        return index

    def candidates(self, stem: str) -> list[PurePath]:
        if self._by_stem is None:
            self._by_stem = self._build()
        return self._by_stem.get(stem, [])

    def best_match(self, parts: tuple[str, ...], *, need_suffix: int = 1) -> PurePath | None:
        """Файл, путь которого совпадает с ``parts`` по самому длинному хвосту.

        ``parts`` — компоненты пути (последний — имя файла, расширение может
        отсутствовать). Совпадение только по имени файла принимается, если
        кандидат единственный.
        """
        if not parts:
            return None
        name = parts[-1]
        stem = PurePath(name).stem if PurePath(name).suffix else name
        best: PurePath | None = None
        best_score = 0
        tie = False
        for candidate in self.candidates(stem):
            if PurePath(name).suffix and candidate.name != name:
                continue
            score = _common_tail(candidate.with_suffix("").parts, _strip_suffix(parts))
            if score > best_score:
                best, best_score, tie = candidate, score, False
            elif score == best_score:
                tie = True
        if best is None or best_score < need_suffix:
            return None
        if tie and best_score == 1:
            return None
        return best

    def find_line(self, rel: PurePath, symbol: str) -> int | None:
        """Первая строка, где ``symbol`` встречается как вызов/объявление."""
        path = self.root / rel
        try:
            if path.stat().st_size > MAX_SCANNED_BYTES:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        pattern = re.compile(rf"\b{re.escape(symbol)}\b\s*[(:=]")
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                return number
        return None


def hints_for_cluster(
    index: ProjectIndex,
    full_names: list[str],
    frames: list[str],
    *,
    limit: int = 3,
) -> list[CodeHint]:
    """До ``limit`` подсказок: сам тест по ``full_name``, затем кадры стека."""
    hints: list[CodeHint] = []
    seen: set[str] = set()

    def add(hint: CodeHint | None) -> None:
        if hint is not None and hint.path not in seen and len(hints) < limit:
            seen.add(hint.path)
            hints.append(hint)

    if full_names:
        add(_hint_from_full_name(index, full_names[0]))
    for frame in frames:
        add(_hint_from_frame(index, frame))
    for full_name in full_names[1:]:
        add(_hint_from_full_name(index, full_name))
    return hints


def _hint_from_full_name(index: ProjectIndex, full_name: str) -> CodeHint | None:
    full_name = full_name.strip()
    if not full_name or " " in full_name.split("::")[0]:
        return None
    if "::" in full_name:
        # pytest node id: tests/api/test_x.py::TestClass::test_method[param]
        file_part, *rest = full_name.split("::")
        match = index.best_match(PurePath(file_part.replace("\\", "/")).parts)
        symbol = _PARAMS_RE.sub("", rest[-1]) if rest else None
    else:
        # ru.x.y.TestClass#method, ru.x.y.TestClass.method, tests.test_x.TestC.test_m
        dotted, _, method = full_name.partition("#")
        parts = [p for p in _PARAMS_RE.sub("", dotted).split(".") if p]
        match = None
        symbol = _PARAMS_RE.sub("", method) or None
        for end in range(len(parts), 0, -1):
            match = index.best_match(tuple(parts[:end]), need_suffix=min(2, end))
            if match is not None:
                if not symbol and end < len(parts):
                    symbol = parts[end] if end == len(parts) - 1 else parts[-1]
                break
    if match is None:
        return None
    line = index.find_line(match, symbol) if symbol else None
    return CodeHint(match.as_posix(), line, "код теста (по full_name)")


def _hint_from_frame(index: ProjectIndex, frame: str) -> CodeHint | None:
    java = _JAVA_FRAME_RE.match(frame)
    if java:
        qualified, filename, line = java.groups()
        package = qualified.split(".")[:-2]
        match = index.best_match((*package, filename), need_suffix=1)
        if match is None:
            return None
        return CodeHint(match.as_posix(), int(line), "кадр стека")
    found = _PATH_FRAME_RE.search(frame)
    if not found:
        return None
    raw_path = found.group("py") or found.group("js")
    line = found.group("pyline") or found.group("jsline")
    parts = PurePath(raw_path.replace("\\", "/")).parts
    match = index.best_match(parts, need_suffix=1)
    if match is None:
        return None
    return CodeHint(match.as_posix(), int(line) if line else None, "кадр стека")


def _strip_suffix(parts: tuple[str, ...]) -> tuple[str, ...]:
    if not parts:
        return parts
    return (*parts[:-1], PurePath(parts[-1]).stem if PurePath(parts[-1]).suffix else parts[-1])


def _common_tail(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    count = 0
    for a, b in zip(reversed(left), reversed(right)):
        if a != b:
            break
        count += 1
    return count
