"""База знаний проекта: запомненные причины и рецепты в ``alla-kb/<id>.json``.

Записи создаются из обратной связи пользователя (команда ``remember``) и
лежат в репозитории автотестов — общие для команды через git. Одна запись —
один файл, ключи и списки отсортированы: так меньше конфликтов при merge.

Проблема узнаётся двумя способами:

* **точно** — стабильная сигнатура кластера (как у серверной exact feedback
  memory) уже подтверждалась для записи;
* **по признаку** — каждая строка короткого признака ошибки (1–3 строки из
  сообщения/трейса/лога) есть в данных кластера. Сравнение без учёта чисел,
  ID, времени и пробелов. В отличие от серверного «message + весь лог»
  короткий признак повторно совпадает, а проверка точная, без нечёткого
  TF-IDF — модель получает факт, а не догадку.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alla_core.knowledge.feedback_signature import (
    build_feedback_cluster_context,
    get_cluster_feedback_sources,
)
from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_core.utils.text_normalization import normalize_text

KB_DIRNAME = "alla-kb"
MAX_MATCHES = 3
MAX_FINGERPRINT_LINES = 3
MAX_FINGERPRINT_LINE_CHARS = 160
CATEGORY_TO_KB = {"тест": "test", "приложение": "service", "окружение": "env", "данные": "data"}
KB_TO_CATEGORY = {value: key for key, value in CATEGORY_TO_KB.items()}

_ID_RE = re.compile(r"^[a-z0-9_]{1,100}$")
_NUMBER_RE = re.compile(r"\b\d+\b|<NUM>")
_TRUNCATION_MARKERS = ("...[обрезано]", "[…]", "…")
_SECRET_RE = re.compile(
    r"bearer\s|authorization|passw(?:or)?d\s*[=:]|token\s*[=:]|secret|api[_-]?key",
    re.IGNORECASE,
)
_FRAME_RE = re.compile(r"^\s*(?:at\s|File\s\"|\.\.\.\s*\d+\s+more)")
_GENERIC_MESSAGE_RE = re.compile(
    r"expected|but was|assertionerror|assertion failed|ожидал|не равно", re.IGNORECASE
)
_LOG_ERROR_RE = re.compile(r"\b(?:ERROR|FATAL|SEVERE)\b|[\w.$]+(?:Exception|Error)\b")
_CONFLICT_RE = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
    strict=True,
))

README = """\
# База знаний alla-launch

Запомненные причины падений и рецепты исправления для скилла Qwen Code
`alla-launch`. Одна запись — один JSON-файл. Записи создаёт команда
`remember` из обратной связи в чате; при следующих разборах скилл узнаёт
проблему по сигнатуре или по признаку (`error_example`) и подсказывает
рецепт.

Коммитьте эту папку, чтобы рецепты получила вся команда. Файлы можно править
руками: `title`, `description`, `resolution_steps`, `error_example`
(1–3 строки из текста ошибки или лога). Поле `id` не меняйте.
"""


# ---------------------------------------------------------------------------
# Сигнатуры и признаки
# ---------------------------------------------------------------------------


def cluster_signature(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> str | None:
    """Стабильная сигнатура кластера ``v<версия>:<hash>`` (или None без данных)."""
    context = build_feedback_cluster_context(cluster, tests_by_id)
    if context is None:
        return None
    signature = context.base_issue_signature
    return f"v{signature.version}:{signature.signature_hash}"


def cluster_evidence(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> tuple[str, str, str]:
    """Сообщение, трейс и лог представителя — источник признаков."""
    return get_cluster_feedback_sources(cluster, tests_by_id)


def normalize_fp(text: str) -> str:
    """Нормализация для признаков: без ID/времени/чисел, пробелы схлопнуты."""
    text = normalize_text(text.replace("\r\n", "\n").replace("\r", "\n"))
    return " ".join(_NUMBER_RE.sub("<#>", text).split())


def fingerprint_lines(fingerprint: str) -> list[str]:
    """Строки признака без маркеров обрезки и оформления."""
    lines: list[str] = []
    for raw in fingerprint.splitlines():
        line = raw
        for marker in _TRUNCATION_MARKERS:
            line = line.replace(marker, " ")
        line = line.strip().strip("`").strip()
        if line:
            lines.append(line)
    return lines


def missing_fingerprint_lines(fingerprint: str, text: str) -> list[str]:
    """Строки признака, которых нет в тексте (пусто — признак совпал)."""
    haystack = normalize_fp(text)
    return [line for line in fingerprint_lines(fingerprint) if normalize_fp(line) not in haystack]


def fingerprint_hits(fingerprint: str, text: str) -> bool:
    return bool(fingerprint_lines(fingerprint)) and not missing_fingerprint_lines(fingerprint, text)


def secret_lines(fingerprint: str) -> list[str]:
    return [line for line in fingerprint_lines(fingerprint) if _SECRET_RE.search(line)]


def default_fingerprint(message: str, trace: str, log: str) -> str:
    """Признак по умолчанию: первая строка ошибки, для общих assertion — плюс строка лога."""
    first = _first_line(message) or next(
        (line.strip() for line in trace.splitlines() if line.strip() and not _FRAME_RE.match(line)),
        "",
    )
    first = _cut(first)
    lines = [first] if first else []
    generic = not first or len(first) < 40 or bool(_GENERIC_MESSAGE_RE.search(first))
    if generic and log:
        for line in log.splitlines():
            if line.startswith("--- ["):
                continue
            match = _LOG_ERROR_RE.search(line)
            if match:
                lines.append(_cut(line[match.start():].strip()))
                break
    return "\n".join(line for line in lines if line)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _cut(line: str) -> str:
    """Обрезать до лимита по границе слова, чтобы не разрезать ID или дату."""
    if len(line) <= MAX_FINGERPRINT_LINE_CHARS:
        return line
    cut = line[:MAX_FINGERPRINT_LINE_CHARS]
    return cut[: cut.rfind(" ")].rstrip() if " " in cut else cut


# ---------------------------------------------------------------------------
# Записи и хранилище
# ---------------------------------------------------------------------------


@dataclass
class KBRecord:
    """Запись базы знаний; поля совместимы с серверным ``KBEntry``."""

    id: str
    title: str
    category: str  # test | service | env | data
    description: str
    resolution_steps: list[str]
    error_example: str
    confirmed_signatures: list[str] = field(default_factory=list)
    rejected_signatures: list[str] = field(default_factory=list)
    created: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KBRecord:
        if data.get("category") not in KB_TO_CATEGORY:
            raise ValueError(f"неизвестная категория {data.get('category')!r}")
        entry_id = str(data["id"])
        if not _ID_RE.match(entry_id):
            raise ValueError(f"некорректный id {entry_id!r}")
        return cls(
            id=entry_id,
            title=str(data["title"]),
            category=str(data["category"]),
            description=str(data.get("description", "")),
            resolution_steps=[str(step) for step in data.get("resolution_steps", [])],
            error_example=str(data.get("error_example", "")),
            confirmed_signatures=sorted({str(s) for s in data.get("confirmed_signatures", [])}),
            rejected_signatures=sorted({str(s) for s in data.get("rejected_signatures", [])}),
            created=dict(data.get("created", {})),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "resolution_steps": self.resolution_steps,
            "error_example": self.error_example,
            "step_path": None,
            "confirmed_signatures": sorted(set(self.confirmed_signatures)),
            "rejected_signatures": sorted(set(self.rejected_signatures)),
            "created": self.created,
        }

    def confirm(self, signature: str) -> None:
        self.confirmed_signatures = sorted(set(self.confirmed_signatures) | {signature})
        self.rejected_signatures = sorted(set(self.rejected_signatures) - {signature})

    def reject(self, signature: str) -> None:
        self.rejected_signatures = sorted(set(self.rejected_signatures) | {signature})
        self.confirmed_signatures = sorted(set(self.confirmed_signatures) - {signature})


def find_kb_dir(project_root: Path) -> Path:
    """``alla-kb`` в корне git-репозитория (или в корне проекта без git)."""
    root = project_root.resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            return candidate / KB_DIRNAME
    return root / KB_DIRNAME


class ProjectKB:
    """Папка ``alla-kb`` с записями базы знаний."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, entry_id: str) -> Path:
        if not _ID_RE.match(entry_id):
            raise ValueError(f"некорректный id записи {entry_id!r}: допустимы a-z, 0-9, _")
        return self.directory / f"{entry_id}.json"

    def load(self) -> tuple[list[KBRecord], list[str]]:
        """Все записи и предупреждения о пропущенных (битых) файлах."""
        records: list[KBRecord] = []
        warnings: list[str] = []
        if not self.directory.is_dir():
            return records, warnings
        for path in sorted(self.directory.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8")
                if _CONFLICT_RE.search(text):
                    raise ValueError("маркеры конфликта git")
                record = KBRecord.from_json(json.loads(text))
                if record.id != path.stem:
                    raise ValueError(f"id {record.id!r} не совпадает с именем файла")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"База знаний: пропущен {path.name} — {exc}")
                continue
            records.append(record)
        return records, warnings

    def get(self, entry_id: str) -> KBRecord | None:
        path = self.path_for(entry_id)
        if not path.is_file():
            return None
        return KBRecord.from_json(json.loads(path.read_text(encoding="utf-8")))

    def save(self, record: KBRecord) -> Path:
        path = self.path_for(record.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        readme = self.directory / "README.md"
        if not readme.exists():
            readme.write_text(README, encoding="utf-8")
        text = json.dumps(record.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8")
        return path


def make_entry_id(title: str, fingerprint: str) -> str:
    """id записи: транслитерация названия + хэш признака. Задаётся один раз."""
    latin = "".join(_TRANSLIT.get(char, char) for char in title.lower())
    base = re.sub(r"[^a-z0-9]+", "_", latin).strip("_")[:60] or "entry"
    suffix = hashlib.sha256(normalize_fp(fingerprint).encode("utf-8")).hexdigest()[:8]
    return f"{base}_{suffix}"


# ---------------------------------------------------------------------------
# Сопоставление кластера с базой знаний
# ---------------------------------------------------------------------------


def match_cluster(
    records: Iterable[KBRecord],
    signature: str | None,
    evidence: str,
) -> list[dict[str, Any]]:
    """Снимки подходящих записей: сначала точные, затем по признаку; не больше 3."""
    exact: list[dict[str, Any]] = []
    by_fingerprint: list[dict[str, Any]] = []
    for record in records:
        if signature and signature in record.rejected_signatures:
            continue
        if signature and signature in record.confirmed_signatures:
            exact.append(_snapshot(record, "exact"))
        elif fingerprint_hits(record.error_example, evidence):
            by_fingerprint.append(_snapshot(record, "fingerprint"))
    return (exact + by_fingerprint)[:MAX_MATCHES]


def _snapshot(record: KBRecord, origin: str) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "category": KB_TO_CATEGORY[record.category],
        "description": record.description,
        "steps": record.resolution_steps,
        "fingerprint": record.error_example,
        "origin": origin,
    }
