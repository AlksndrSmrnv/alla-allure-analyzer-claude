"""База знаний проекта: запомненные причины и рецепты в ``alla-kb/<id>.json``.

Записи создаются из обратной связи пользователя (команда ``remember``) и
лежат в репозитории автотестов — общие для команды через git. Одна запись —
один файл, ключи и списки отсортированы: так меньше конфликтов при merge.

Проблема узнаётся двумя способами:

* **точно** — стабильная сигнатура кластера (:mod:`alla_skill_lib.signature`) уже
  подтверждалась для записи;
* **по признаку** — каждая строка короткого признака ошибки (1–3 строки из
  сообщения/трейса/лога) есть в данных кластера. Сравнение без учёта чисел,
  ID, времени и пробелов. Короткий признак (а не «message + весь лог»)
  повторно совпадает, а проверка точная, без нечёткого TF-IDF — модель
  получает факт, а не догадку.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_core.utils.text_normalization import canonicalize_kb_error_example, normalize_text
from alla_skill_lib import workspace as ws
from alla_skill_lib.code_hints import SKIP_DIRS
from alla_skill_lib.signature import SIGNATURE_VERSION
from alla_skill_lib.signature import cluster_signature as cluster_signature
from alla_skill_lib.signature import cluster_sources

KB_DIRNAME = "alla-kb"
MAX_MATCHES = 3
# Тестов в записи — не больше: список коммитится, а для узнавания хватает недавних.
MAX_RECORD_TESTS = 50
MAX_FINGERPRINT_LINES = 3
MAX_FINGERPRINT_LINE_CHARS = 160
CATEGORY_TO_KB = {"тест": "test", "приложение": "service", "окружение": "env", "данные": "data"}
KB_TO_CATEGORY = {value: key for key, value in CATEGORY_TO_KB.items()}

_ID_RE = re.compile(r"^[a-z0-9_]{1,100}$")
_NUMBER_RE = re.compile(r"\b\d+\b|<NUM>")
_TRUNCATION_MARKERS = ("...[обрезано]", "[…]", "…")
# Секрет как ключ со значением: password=…, "access_token": "…" (кавычка между
# ключом и разделителем), Authorization: …, «Bearer <значение>», JWT, пароль в
# URL, ключи AWS/GitHub/Slack, PEM. «token: expired» и «Authorization failed» —
# обычный текст ошибки: слово-ключ через «:» считается секретом только для
# «сильных» ключей (password, secret, api-key) или если значение похоже на
# токен (цифра или 16+ символов).
_STRONG_KEY = r"(?:passw(?:or)?d|pwd|пароль|secret|секрет|api[_-]?key|private[_-]?key)"
_WEAK_KEY = r"(?:token|токен|session[_-]?id)"
_SECRET_RE = re.compile(
    r"\bbearer\s+\S"
    r"|\b(?:authorization|cookie)[\"']?\s*[=:]"
    rf"|(?:{_STRONG_KEY}|{_WEAK_KEY})[\"']?\s*=\s*\S"
    rf"|{_STRONG_KEY}[\"']?\s*:\s*\S"
    rf"|{_WEAK_KEY}[\"']?\s*:\s*[\"']?(?=\S*\d|\S{{16}})\S+"
    rf"|(?:{_STRONG_KEY}|{_WEAK_KEY})[\"']\s*:\s*[\"']"
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}"
    r"|://[^/\s:@]+:[^/\s@]+@"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
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

_KNOWN_FIELDS = frozenset({
    "id", "title", "category", "description", "resolution_steps", "error_example",
    "confirmed_signatures", "rejected_signatures", "confirmed_tests", "rejected_tests", "created",
})

README = """\
# База знаний alla-launch

Запомненные причины падений и рецепты исправления для скилла Qwen Code
`alla-launch`. Одна запись — один JSON-файл. Записи создаёт команда
`remember` из обратной связи в чате; при следующих разборах скилл узнаёт
проблему по сигнатуре или по признаку (`error_example`) и подсказывает
рецепт.

Коммитьте эту папку, чтобы рецепты получила вся команда. Файлы можно править
руками: `title`, `description`, `resolution_steps`, `error_example`
(1–3 строки из текста ошибки или лога), `confirmed_tests` и `rejected_tests`
(тесты, на которых запись подтвердили или отвергли: по ним запись узнаётся и после
обновления скилла). Поле `id` не меняйте.
"""


# ---------------------------------------------------------------------------
# Сигнатуры и признаки
# ---------------------------------------------------------------------------


def cluster_evidence(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> tuple[str, str, str]:
    """Сообщение, трейс и лог представителя — источник признаков."""
    return cluster_sources(cluster, tests_by_id)


def normalize_fp(text: str) -> str:
    """Нормализация для признаков: без ID/времени/чисел, пробелы схлопнуты."""
    text = normalize_text(text.replace("\r\n", "\n").replace("\r", "\n"))
    return " ".join(_NUMBER_RE.sub("<#>", _EMAIL_RE.sub("<EMAIL>", text)).split())


def store_fingerprint(fingerprint: str) -> str:
    """Признак для записи в ``alla-kb/``: без ID, дат и адресов почты.

    ``alla-kb/`` коммитится, а в тексте ошибки бывают UUID, e-mail и другие
    данные конкретного прогона. Сравнение нормализует обе стороны, поэтому
    сохранённая нормализованная строка совпадает так же, как исходная.
    """
    text = canonicalize_kb_error_example("\n".join(fingerprint_lines(fingerprint)))
    return _EMAIL_RE.sub("<EMAIL>", text)


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
    return _missing_in(fingerprint, normalize_fp(text))


def _missing_in(fingerprint: str, haystack: str) -> list[str]:
    """То же для уже нормализованного текста: его нормализуют один раз на кластер."""
    return [line for line in fingerprint_lines(fingerprint) if normalize_fp(line) not in haystack]


def fingerprint_hits(fingerprint: str, text: str) -> bool:
    return bool(fingerprint_lines(fingerprint)) and not missing_fingerprint_lines(fingerprint, text)


def secret_lines(text: str) -> list[str]:
    """Строки, похожие на секреты: их нельзя класть в коммитируемый ``alla-kb/``."""
    return [line.strip() for line in text.splitlines() if _SECRET_RE.search(line)]


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
    """Запись базы знаний ``alla-kb/<id>.json``."""

    id: str
    title: str
    category: str  # test | service | env | data
    description: str
    resolution_steps: list[str]
    error_example: str
    confirmed_signatures: list[str] = field(default_factory=list)
    rejected_signatures: list[str] = field(default_factory=list)
    # Тесты, на которых запись подтвердили или отвергли (``key_of_test``). Имя теста от версии
    # сигнатуры не зависит: по нему запись узнаётся, когда хэши старой версии не совпадают.
    confirmed_tests: list[str] = field(default_factory=list)
    rejected_tests: list[str] = field(default_factory=list)
    created: dict[str, Any] = field(default_factory=dict)
    # Поля, добавленные руками (jira, owner, tags…): при перезаписи не теряются.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Any) -> KBRecord:
        """Разобрать запись; любая ошибка структуры — ``ValueError``."""
        if not isinstance(data, dict):
            raise ValueError(f"запись должна быть JSON-объектом, а не {type(data).__name__}")
        for key in ("id", "title", "category"):
            if key not in data:
                raise ValueError(f"нет поля {key!r}")
        for key in ("resolution_steps", "confirmed_signatures", "rejected_signatures",
                    "confirmed_tests", "rejected_tests"):
            if not isinstance(data.get(key, []), list):
                raise ValueError(f"поле {key!r} должно быть списком")
        if not isinstance(data.get("created", {}), dict):
            raise ValueError("поле 'created' должно быть объектом")
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
            confirmed_tests=sorted({str(t) for t in data.get("confirmed_tests", [])}),
            rejected_tests=sorted({str(t) for t in data.get("rejected_tests", [])}),
            created=dict(data.get("created", {})),
            extra={key: value for key, value in data.items() if key not in _KNOWN_FIELDS},
        )

    def to_json(self) -> dict[str, Any]:
        return {
            **self.extra,
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "resolution_steps": self.resolution_steps,
            "error_example": self.error_example,
            "confirmed_signatures": sorted(set(self.confirmed_signatures)),
            "rejected_signatures": sorted(set(self.rejected_signatures)),
            "confirmed_tests": sorted(set(self.confirmed_tests)),
            "rejected_tests": sorted(set(self.rejected_tests)),
            "created": self.created,
        }

    def confirm(self, signature: str, tests: Iterable[str] = ()) -> None:
        """Запись подходит ошибке с этой сигнатурой и этим тестам."""
        tests = set(tests)
        self.confirmed_signatures = sorted(set(self.confirmed_signatures) | {signature})
        self.rejected_signatures = sorted(set(self.rejected_signatures) - {signature})
        self.confirmed_tests = _merge_tests(self.confirmed_tests, tests)
        self.rejected_tests = sorted(set(self.rejected_tests) - tests)

    def reject(self, signature: str, tests: Iterable[str] = ()) -> None:
        """Запись не подходит ошибке с этой сигнатурой и этим тестам."""
        tests = set(tests)
        self.rejected_signatures = sorted(set(self.rejected_signatures) | {signature})
        self.confirmed_signatures = sorted(set(self.confirmed_signatures) - {signature})
        self.rejected_tests = _merge_tests(self.rejected_tests, tests)
        self.confirmed_tests = sorted(set(self.confirmed_tests) - tests)


def _merge_tests(old: Iterable[str], new: set[str]) -> list[str]:
    """Старые и новые тесты, не больше ``MAX_RECORD_TESTS``: новые важнее старых."""
    fresh = sorted(new)[:MAX_RECORD_TESTS]
    rest = [test for test in sorted(set(old)) if test not in new]
    return sorted([*fresh, *rest][:MAX_RECORD_TESTS])


def key_of_test(full_name: str | None, test_case_id: object = None) -> str | None:
    """Ключ теста для записи: ``full_name`` (класс и метод), иначе ``testcase:<id>`` TestOps.

    Параметры теста в ключ не входят: на разных параметрах это та же проблема.
    """
    if full_name and full_name.strip():
        return full_name.strip()
    if test_case_id is not None and str(test_case_id).strip():
        return f"testcase:{str(test_case_id).strip()}"
    return None


def cluster_tests(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> list[str]:
    """Ключи тестов кластера (``prepare``: модели ядра)."""
    keys = {
        key_of_test(test.full_name, test.test_case_id)
        for test in (tests_by_id.get(test_id) for test_id in cluster.member_test_ids)
        if test is not None
    }
    return sorted(key for key in keys if key)


def entry_tests(run: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    """Ключи тестов кластера по ``run.json`` (после ``prepare``: ``remember``, отчёт)."""
    clusters = (run.get("clustering") or {}).get("clusters", [])
    members: list[Any] = next((cluster.get("member_test_ids", []) for cluster in clusters
                               if cluster.get("cluster_id") == entry.get("cluster_id")), [])
    by_id = {test.get("test_result_id"): test
             for test in (run.get("triage") or {}).get("failed_tests", [])}
    keys = {key_of_test(test.get("full_name"), test.get("test_case_id"))
            for test in (by_id.get(test_id) for test_id in members) if test is not None}
    return sorted(key for key in keys if key)


def find_kb_dir(project_root: Path) -> Path:
    """``alla-kb`` в корне git-репозитория (или в корне проекта без git)."""
    root = project_root.resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            return candidate / KB_DIRNAME
    return root / KB_DIRNAME


def kb_dir_for(project_root: Path, module: str = "") -> Path:
    """База знаний модуля: ``<модуль>/alla-kb``; без модуля — ``find_kb_dir``.

    Модуль — путь от корня проекта (см. ``alla_skill_lib.modules``). Кластеры
    разных модулей так не видят записей друг друга.
    """
    if not module:
        return find_kb_dir(project_root)
    return project_root.resolve() / module / KB_DIRNAME


def kb_label(project_root: Path, kb_dir: Path) -> str:
    """Как показывать базу знаний человеку: ``orders/alla-kb`` или просто ``alla-kb``."""
    try:
        return kb_dir.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:  # корень git выше корня проекта
        return kb_dir.name


def discover_kb_dirs(project_root: Path) -> list[Path]:
    """Все папки ``alla-kb`` внутри проекта (в служебных и собранных папках не ищем)."""
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(project_root):
        if KB_DIRNAME in dirnames:
            found.append(Path(dirpath) / KB_DIRNAME)
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and name != KB_DIRNAME]
    return sorted(found)


class ProjectKB:
    """Папка ``alla-kb`` с записями базы знаний."""

    def __init__(self, directory: Path, label: str | None = None) -> None:
        self.directory = directory
        self.label = label or directory.name  # для сообщений: ``orders/alla-kb``

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
                records.append(self._read(path))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"База знаний: пропущен {self.label}/{path.name} — {exc}")
        return records, warnings

    def get(self, entry_id: str) -> KBRecord | None:
        path = self.path_for(entry_id)
        if not path.is_file():
            return None
        return self._read(path)

    @staticmethod
    def _read(path: Path) -> KBRecord:
        """Запись из файла; конфликт слияния и несовпадение id — ``ValueError``."""
        text = path.read_text(encoding="utf-8-sig")
        if _CONFLICT_RE.search(text):
            raise ValueError("маркеры конфликта git — разреши конфликт в файле")
        record = KBRecord.from_json(json.loads(text))
        if record.id != path.stem:
            raise ValueError(f"id {record.id!r} не совпадает с именем файла")
        return record

    def save(self, record: KBRecord) -> Path:
        path = self.path_for(record.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        readme = self.directory / "README.md"
        if not readme.exists():
            ws.write_atomic(readme, README)
        text = json.dumps(record.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ws.write_atomic(path, text)
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
    tests: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Снимки подходящих записей: сначала точные, затем по признаку; не больше 3."""
    exact: list[dict[str, Any]] = []
    by_fingerprint: list[dict[str, Any]] = []
    haystack = normalize_fp(evidence)  # один раз на кластер, а не на каждую запись
    tests = list(tests)
    for record in records:
        origin = record_matches(record, signature, haystack, tests)
        if origin == "exact":
            exact.append(_snapshot(record, origin))
        elif origin == "fingerprint":
            by_fingerprint.append(_snapshot(record, origin))
    return (exact + by_fingerprint)[:MAX_MATCHES]


def record_matches(
    record: KBRecord,
    signature: str | None,
    haystack: str,
    tests: Iterable[str] = (),
) -> str | None:
    """Как запись подходит кластеру: ``exact`` | ``fingerprint`` | ``None``.

    ``haystack`` — данные кластера после ``normalize_fp``, ``tests`` — ключи его тестов
    (``key_of_test``). Одно правило для подбора записей в ``prepare`` и для проверки
    известной проблемы при сборке отчёта.

    Тесты записи действуют, только пока у неё нет сигнатуры текущей версии того же вида
    (подтверждённой или отвергнутой): после смены версии хэши в записи не совпадают ни с
    чем, и подтверждение или отказ держатся на тесте и признаке. Без признака тест не
    решает ничего — тест падает и по другим причинам.
    """
    tests = set(tests)
    hits = bool(fingerprint_lines(record.error_example)) and not _missing_in(
        record.error_example, haystack)
    if signature and signature in record.confirmed_signatures:
        return "exact"
    if record_rejected(record, signature, tests, hits=hits):
        return None
    if hits and tests & set(record.confirmed_tests) and not _has_current(record.confirmed_signatures):
        return "exact"
    return "fingerprint" if hits else None


def record_rejected(
    record: KBRecord,
    signature: str | None,
    tests: Iterable[str],
    *,
    hits: bool,
) -> bool:
    """Запись отвергнута для этого кластера: по сигнатуре или, после смены версии, по тесту.

    ``hits`` — признак записи есть в данных кластера.
    """
    if signature and signature in record.rejected_signatures:
        return True
    return (hits and bool(set(tests) & set(record.rejected_tests))
            and not _has_current(record.rejected_signatures))


def _has_current(signatures: Iterable[str]) -> bool:
    """Есть сигнатура текущей версии: тесты записи тогда не нужны."""
    prefix = f"v{SIGNATURE_VERSION}:"
    return any(signature.startswith(prefix) for signature in signatures)


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
