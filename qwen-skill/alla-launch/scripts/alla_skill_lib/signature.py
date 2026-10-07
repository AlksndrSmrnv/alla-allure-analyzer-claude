"""Стабильная сигнатура проблемы ``v<версия>:<hash>``: точное узнавание и повторы.

Сигнатура держится на том, что называет причину падения:

* трейс есть и ошибка сама называет причину (исключение не-ассерт или ``Caused by``) —
  сообщение и якорь трейса, лог не входит;
* трейс есть, а ошибка — общий ассерт (:data:`GENERIC_ASSERTION_RE`) без ``Caused by`` —
  к сообщению и трейсу добавляются строки-ошибки лога: у ``expected: <200> but was: <500>``
  причину называет только лог. Лог из одних обычных строк (INFO) не добавляется — шум
  сделал бы сигнатуру разной от прогона к прогону;
* трейса нет — сообщение и якорь лога (если ошибок нет — первые обычные строки).

Время, UUID, длинные числа и имена потоков в материал не входят, коды ошибок
(``numeric_codes``) — входят.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_core.utils.log_events import strip_source_marks
from alla_core.utils.log_focus import strip_log_selection_metadata
from alla_core.utils.log_utils import parse_log_sections
from alla_core.utils.text_normalization import (
    NON_CODE_PREFIXES,
    normalize_text,
    normalize_text_for_llm,
    numeric_codes,
    replace_thread_names,
)

# Префикс версии не даёт старому хэшу совпасть с новым при смене материала.
SIGNATURE_VERSION = 7

# Признаки общего ассерта: ошибка проверки, которая не называет причину. Проверяются по
# строкам сообщения и трейса (без кадров и префикса pytest «E »); Hamcrest — через строку.
GENERIC_ASSERTION_RE = re.compile(
    r"^(?:"
    r"(?:java\.lang\.|kotlin\.)?AssertionError\b"
    r"|org\.opentest4j\.AssertionFailedError\b"
    r"|org\.junit\.ComparisonFailure\b"
    r"|junit\.framework\.(?:AssertionFailedError|ComparisonFailure)\b"
    r"|org\.assertj\.[\w.$]+"
    r"|assert\s"
    r"|Expected:.*\n\s*but:"
    r"|.*\bexpected\b.*\bbut (?:was|found)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_MAX_MESSAGE_WORDS = 10
_MAX_MESSAGE_CHARS = 120
_MAX_TRACE_LINES = 4
_MAX_LOG_ERROR_LINES = 6
_MAX_LOG_PLAIN_LINES = 3
_MAX_CODES = 4

_STACK_FRAME_RE = re.compile(r"^\s*(?:at\s+\S+\(|\.\.\.\s+\d+\s+more\b|File \".+\", line \d+)")
_PYTEST_PREFIX_RE = re.compile(r"^E\s+")
_CAUSED_BY_RE = re.compile(r"^\s*Caused by:", re.MULTILINE)
_EXCEPTION_LINE_RE = re.compile(r"^[\w$]+(?:\.[\w$]+)*(?:Exception|Error|Throwable)\b")
_CAUSE_HINT_RE = re.compile(
    r"(?:Caused by|Traceback)\b|\b[\w.$]+(?:Exception|Error)\b", re.IGNORECASE)
_ERROR_HINT_RE = re.compile(
    r"\b(?:ERROR|FATAL|SEVERE|CRITICAL)\b|(?:FAILED|Failed to)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_$.:-]*")


# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------


def _log_evidence(summary: FailedTestSummary) -> str:
    """Лог как материал признаков: без пометок отбора и строк источника."""
    snippet = strip_source_marks(summary.log_snippet or "")
    if summary.log_selection_truncated:
        snippet = strip_log_selection_metadata(snippet)
    return snippet.strip()


def _cluster_logs(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> list[str]:
    """Логи кластера: представитель первым, затем остальные участники."""
    ids = [cluster.representative_test_id] if cluster.representative_test_id is not None else []
    logs: list[str] = []
    for test_id in dict.fromkeys([*ids, *cluster.member_test_ids]):
        test = tests_by_id.get(test_id)
        log = _log_evidence(test) if test else ""
        if log:
            logs.append(log)
    return logs


def cluster_sources(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> tuple[str, str, str]:
    """Сообщение, трейс и лог представителя — материал сигнатуры и признака базы знаний."""
    representative = (tests_by_id.get(cluster.representative_test_id)
                      if cluster.representative_test_id is not None else None)
    message = (representative.status_message if representative and representative.status_message
               else cluster.example_message) or ""
    trace = (representative.status_trace if representative and representative.status_trace
             else cluster.example_trace_snippet) or ""
    logs = _cluster_logs(cluster, tests_by_id)
    return message, trace, logs[0] if logs else ""


# ---------------------------------------------------------------------------
# Общий ассерт
# ---------------------------------------------------------------------------


def _error_lines(text: str) -> list[str]:
    """Непустые строки без кадров стека и префикса pytest «E »."""
    lines = (_PYTEST_PREFIX_RE.sub("", line.strip()) for line in text.splitlines())
    return [line for line in lines if line and not _STACK_FRAME_RE.match(line)]


def is_generic_assertion(message: str, trace: str) -> bool:
    """Ошибка — общий ассерт без ``Caused by``: причину называет не она, а лог.

    Нужен признак из :data:`GENERIC_ASSERTION_RE` и ни одной строки с классом
    исключения не из этого списка (``ConnectionError`` внутри ``assert …`` — не ассерт).
    """
    if _CAUSED_BY_RE.search(trace):
        return False
    lines = _error_lines(message) + _error_lines(trace)
    if not GENERIC_ASSERTION_RE.search("\n".join(lines)):
        return False
    return not any(_EXCEPTION_LINE_RE.match(line) and not GENERIC_ASSERTION_RE.match(line)
                   for line in lines)


# ---------------------------------------------------------------------------
# Нормализация и якоря
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split()).casefold()


def _strict(text: str) -> str:
    """Строгий вид: без времени, UUID и потоков, числа остаются (короткое сообщение)."""
    return _collapse(normalize_text_for_llm(replace_thread_names(text)))


def _soft(text: str) -> str:
    """Мягкий вид: ещё и без длинных чисел (ID), но с кодами ошибок отдельным отпечатком."""
    normalized = _collapse(normalize_text(replace_thread_names(text)))
    codes = numeric_codes(text)
    if not codes:
        return normalized
    return f"{normalized} <codes:{'|'.join(sorted(codes[:_MAX_CODES]))}>"


def _has_signal_words(text: str) -> bool:
    words = (word.casefold() for word in _WORD_RE.findall(_collapse(normalize_text(text))))
    return sum(word not in NON_CODE_PREFIXES for word in words) >= 2


def _unique(lines: list[str]) -> list[str]:
    """Строки в виде сигнатуры: без повторов и по порядку — не зависят от числа повторов."""
    return sorted({_soft(line) for line in lines} - {""})


def _message_part(message: str) -> str:
    strict = _strict(message)
    short = len(strict.split()) <= _MAX_MESSAGE_WORDS and len(strict) <= _MAX_MESSAGE_CHARS
    return strict if short else _soft(message)


def _trace_part(trace: str) -> list[str]:
    """Первая строка ошибки и строки причин (``Caused by``) — без кадров стека."""
    lines = _error_lines(trace)
    if not lines:
        return []
    head, rest = _soft(lines[0]), [line for line in lines[1:] if _CAUSE_HINT_RE.search(line)]
    return [head, *[line for line in _unique(rest) if line != head][:_MAX_TRACE_LINES - 1]]


@dataclass(frozen=True)
class _LogAnchor:
    causes: list[str]  # строки с исключением, Caused by, Traceback
    errors: list[str]  # ERROR/FATAL/FAILED со смыслом
    plain: list[str]   # остальное

    def error_lines(self) -> list[str]:
        """Первая причина, первая ошибка, затем остальные причины и ошибки."""
        selected = self.causes[:1] + self.errors[:1]
        selected += self.causes[1:1 + _MAX_LOG_ERROR_LINES - len(selected)]
        selected += self.errors[1:1 + _MAX_LOG_ERROR_LINES - len(selected)]
        return selected

    def lines(self, *, with_plain: bool) -> list[str]:
        errors = self.error_lines()
        if errors or not with_plain:
            return errors
        return self.plain[:_MAX_LOG_PLAIN_LINES]

    def rank(self) -> tuple[bool, bool, list[str]]:
        return not self.causes, not self.errors, self.lines(with_plain=True)


def _log_anchor(log: str) -> _LogAnchor:
    causes: list[str] = []
    errors: list[str] = []
    plain: list[str] = []
    for _, body in parse_log_sections(log, include_http=False):
        for line in _error_lines(body):
            if _CAUSE_HINT_RE.search(line):
                causes.append(line)
            elif _ERROR_HINT_RE.search(line) and _has_signal_words(line):
                errors.append(line)
            else:
                plain.append(line)
    return _LogAnchor(_unique(causes), _unique(errors), _unique(plain))


def _log_part(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
    *,
    with_plain: bool,
) -> list[str]:
    """Якорь лога представителя; без его лога — самый содержательный у участников."""
    representative = (tests_by_id.get(cluster.representative_test_id)
                      if cluster.representative_test_id is not None else None)
    representative_log = _log_evidence(representative) if representative else ""
    if representative_log:
        return _log_anchor(representative_log).lines(with_plain=with_plain)
    anchors = [anchor for anchor in map(_log_anchor, _cluster_logs(cluster, tests_by_id))
               if anchor.lines(with_plain=True)]
    return min(anchors, key=_LogAnchor.rank).lines(with_plain=with_plain) if anchors else []


# ---------------------------------------------------------------------------
# Сигнатура
# ---------------------------------------------------------------------------


def signature_material(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> str | None:
    """Нормализованный материал сигнатуры: основа (``message+trace+log``…) и части."""
    message, trace, _ = cluster_sources(cluster, tests_by_id)
    parts: dict[str, str] = {}
    if message.strip():
        parts["message"] = _message_part(message)
    trace_lines = _trace_part(trace)
    if trace_lines:
        parts["trace"] = "\n".join(trace_lines)
        log_lines = (_log_part(cluster, tests_by_id, with_plain=False)
                     if is_generic_assertion(message, trace) else [])
    else:
        log_lines = _log_part(cluster, tests_by_id, with_plain=True)
    if log_lines:
        parts["log"] = "\n".join(log_lines)
    if not parts:
        return None
    return "+".join(parts) + "\n" + "\n---\n".join(parts.values())


def cluster_signature(
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
) -> str | None:
    """Стабильная сигнатура кластера ``v<версия>:<hash>`` (или None без данных)."""
    material = signature_material(cluster, tests_by_id)
    if material is None:
        return None
    digest = hashlib.sha256(f"v{SIGNATURE_VERSION}\n{material}".encode()).hexdigest()
    return f"v{SIGNATURE_VERSION}:{digest}"
