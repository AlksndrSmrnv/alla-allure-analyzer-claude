"""Сервис кластеризации похожих ошибок тестов по корневой причине.

Алгоритм: message-first подход.
1. Для каждого падения строятся четыре канала текста:
   - message-document: status_message + category
   - trace-document: status_trace (с компактацией длинных трасс)
   - log-document: log_snippet (лог приложения, если прикреплён к тесту)
   - step-document: failed_step_path (хлебные крошки до упавшего шага)
2. Минимальная нормализация: замена волатильных данных (UUID, timestamps,
   длинные числа, IP) плейсхолдерами.
3. TF-IDF + cosine similarity по каждому каналу отдельно.
4. Gates (применяются в этом порядке, до взвешенного объединения):
   - assertion-actual gate: разные «but was» в assertion → не сливать.
   - step-path hard gate: если у обоих есть непустой step path и step
     similarity < `step_path_strict_threshold`, пара принудительно не
     сливается. Применяется ДО message/log, поэтому log override его не
     обходит. Если разделение оказалось ошибочным, пользователь сводит
     кластера через merge rules (`rule_kind="step"`).
   - log gate: у обоих в логе (секции «файл») есть ошибки, общего ключа
     ошибки нет (корневой класс исключения, код ошибки) и TF-IDF похожесть
     документов ошибок (без кадров, времени, ID, потоков и цифр) ниже
     `log_split_threshold` — пара не сливается. Одинаковый симптом с разными
     ошибками сервиса (пул БД и NPE за одним 500) — разные проблемы.
5. Итоговая similarity для пары:
   - если message есть у обоих и message similarity ниже порога:
     * если у обоих есть лог и log similarity ≥ порога — log override:
       лог становится доминирующим каналом (0.6 log + 0.2 msg + 0.2 trace)
     * иначе пара не может быть склеена (message gate)
   - иначе взвешенная комбинация message/trace/log
   - если message у одного/обоих пустой, fallback на trace (+log)
   - если лога нет у одного/обоих тестов, его вес перераспределяется на message
   - мягкий step-path штраф работает только для пар выше strict threshold
     (или при отключённом gate) и даёт незначительную поправку.
6. Агломеративная кластеризация (complete linkage) по итоговой distance.
"""

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from alla_core.models.clustering import (
    ClusterExample,
    ClusteringReport,
    ClusterSignature,
    FailureCluster,
)
from alla_core.models.testops import FailedTestSummary
from alla_core.utils.log_events import SOURCE_MARK_RE, strip_source_marks
from alla_core.utils.log_focus import strip_log_selection_metadata
from alla_core.utils.log_utils import extract_correlation_from_log
from alla_core.utils.step_paths import normalize_step_path
from alla_core.utils.text_normalization import (
    normalize_text,
    numeric_codes,
    replace_thread_names,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClusteringConfig:
    """Параметры алгоритма кластеризации."""

    similarity_threshold: float = 0.60

    tfidf_max_features: int = 1000
    tfidf_ngram_range: tuple[int, int] = (1, 2)
    message_similarity_weight: float = 0.85
    trace_similarity_weight: float = 0.15
    log_similarity_weight: float = 0.0
    trace_compact_head_lines: int = 30
    trace_compact_tail_lines: int = 30
    log_compact_head_lines: int = 50
    log_compact_tail_lines: int = 50

    max_label_length: int = 120
    trace_snippet_lines: int = 5

    step_path_mismatch_penalty: float = 0.45
    step_path_log_reduction: float = 0.5
    # Hard gate: пара с обоюдно непустыми step paths и step_sim ниже порога
    # принудительно не сливается (final_sim=0). Применяется до message/log
    # gates и НЕ обходится log override. Значение 0.0 фактически отключает
    # gate (sim < 0.0 невозможно), 1.0 — любое отличие шага режет пару.
    step_path_strict_threshold: float = 0.95
    # Gate по логу: оба лога содержат ошибки, общего ключа ошибки (корневой класс, код)
    # нет и документы ошибок похожи меньше порога — пара не сливается. 0.30 — половина
    # порога «логи одинаковые» (log override, 0.60): ниже лог разделяет, выше 0.60
    # объединяет, между ними решают веса. Разные ошибки корпуса — не выше 0.17, одна ошибка
    # с шумом (потоки, строки, ID, посторонние ошибки) — от 0.39. 0.0 отключает gate.
    log_split_threshold: float = 0.30

    @property
    def distance_threshold(self) -> float:
        """Перевод similarity_threshold в distance_threshold для scipy."""
        return 1.0 - self.similarity_threshold


# ---------------------------------------------------------------------------
# Извлечение actual-значения из assertion
# ---------------------------------------------------------------------------

# Матчит: "but was: <Y>", "but was: [Y]", "but was: \"Y\"", "but was:<Y>"
# и русский вариант: "но было: <Y>", "но было: [Y]", "но было: \"Y\""
_ASSERTION_ACTUAL_RE = re.compile(
    r"(?:but\s+was|но\s+было)\s*:?\s*"
    r"(?:<([^>]+)>|\[([^\]]+)\]|\"([^\"]+)\")"
    r"|"
    r"(?:^|\n)\s*(?:actual|фактическое)\s*:\s*(.+?)\s*(?:\n|$)",
    re.IGNORECASE,
)


def _extract_assertion_actual(message: str) -> str | None:
    """Извлечь «actual» значение из assertion-паттерна в сообщении об ошибке.

    Возвращает строку-значение или None если паттерн не найден.
    """
    m = _ASSERTION_ACTUAL_RE.search(message)
    if m is None:
        return None
    raw = m.group(1) or m.group(2) or m.group(3) or m.group(4)
    return " ".join(raw.split()) if raw else raw


# ---------------------------------------------------------------------------
# Фильтр HTTP-секций только с корреляционными ID
# ---------------------------------------------------------------------------

_LOG_SECTION_HEADER_RE = re.compile(
    r"^---\s*\[(?P<section_type>[^\]:\s][^\]:]*?):\s.+?\]\s*---$", re.MULTILINE
)


def _strip_correlation_only_http_sections(log_snippet: str) -> str:
    """Убрать HTTP-секции, содержащие только корреляционные ID.

    Секция считается correlation-only если:
    - заголовок начинается с ``--- [HTTP:``
    - все непустые строки тела начинаются с ``Корреляция:``

    Секции ``--- [файл: ...]`` всегда сохраняются.
    Лог без секционных заголовков возвращается как есть.
    """
    headers = list(_LOG_SECTION_HEADER_RE.finditer(log_snippet))
    if not headers:
        return log_snippet

    kept_parts: list[str] = []
    for idx, match in enumerate(headers):
        header_line = match.group(0)
        body_start = match.end()
        body_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(log_snippet)
        body = log_snippet[body_start:body_end].strip()

        if "[HTTP:" in header_line:
            # Проверить: все ли непустые строки тела — Корреляция:
            body_lines = [line for line in body.splitlines() if line.strip()]
            if body_lines and all(line.strip().startswith("Корреляция:") for line in body_lines):
                continue  # только корреляция → убрать

        kept_parts.append(f"{header_line}\n{body}")

    return "\n\n".join(kept_parts)


def _build_message_document(failure: FailedTestSummary) -> str:
    """Собрать message-документ (message + category)."""
    parts: list[str] = []

    if failure.status_message:
        parts.append(failure.status_message)

    if failure.category:
        parts.append(failure.category)

    raw = "\n".join(parts)
    return normalize_text(raw) if raw else ""


def _build_step_document(failure: FailedTestSummary) -> str:
    """Собрать step-документ из breadcrumb упавшего шага."""
    return normalize_step_path(failure.failed_step_path)


def _compact_trace(trace: str, head_lines: int, tail_lines: int) -> str:
    """Сжать длинный stack trace: оставить head и tail непустых строк."""
    lines = [line for line in trace.splitlines() if line.strip()]
    if not lines:
        return ""

    head = max(head_lines, 0)
    tail = max(tail_lines, 0)
    if head == 0 and tail == 0:
        return ""

    if tail == 0:
        return "\n".join(lines[:head])
    if head == 0:
        return "\n".join(lines[-tail:])
    if len(lines) <= head + tail:
        return "\n".join(lines)
    return "\n".join(lines[:head] + lines[-tail:])


def _build_trace_document(
    failure: FailedTestSummary,
    *,
    head_lines: int,
    tail_lines: int,
) -> str:
    """Собрать trace-документ из status_trace с предварительной компактацией."""
    if not failure.status_trace:
        return ""
    compacted = _compact_trace(
        failure.status_trace,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )
    return normalize_text(compacted) if compacted else ""


def _build_log_document(
    failure: FailedTestSummary,
    *,
    head_lines: int,
    tail_lines: int,
) -> str:
    """Собрать log-документ из log_snippet с предварительной компактацией.

    Перед компактацией убираются HTTP-секции, содержащие только
    корреляционные ID — они одинаковы после нормализации и создают
    ложную схожесть между несвязанными тестами.
    """
    if not failure.log_snippet:
        return ""
    # Строки источника — метаданные: в лог-канал кластеризации не входят.
    snippet = strip_source_marks(failure.log_snippet)
    if failure.log_selection_truncated:
        snippet = strip_log_selection_metadata(snippet)
    filtered = _strip_correlation_only_http_sections(snippet)
    if not filtered.strip():
        return ""
    compacted = _compact_trace(
        filtered,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )
    return normalize_text(compacted) if compacted else ""


# Кадры стека Java и Python: у одной ошибки различаются путём вызова, у разных — совпадают
# пакетами, поэтому в документ ошибок лога не входят.
_STACK_FRAME_RE = re.compile(r"^\s*(?:at\s+\S|\.\.\.\s+\d+\s+more\b|File \".+\", line \d+)")
_EXCEPTION_CLASS_RE = re.compile(
    r"\b(?:[A-Za-z_$][\w$]*\.)*([A-Z][\w$]*(?:Exception|Error|Throwable))\b")
# Как token_pattern TfidfVectorizer: документ без таких слов дал бы нулевой вектор.
_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")


def _file_sections(snippet: str) -> list[str]:
    """Тела секций ``--- [файл: …] ---``; лог без заголовков — одна такая секция.

    В них ``ErrorBlocksHandler`` оставил только события-ошибки. Секции ``HTTP`` и
    ``журнал`` (весь JSON-массив, с INFO) в документ ошибок не входят.
    """
    headers = list(_LOG_SECTION_HEADER_RE.finditer(snippet))
    if not headers:
        return [snippet]
    return [
        snippet[match.end():headers[idx + 1].start() if idx + 1 < len(headers) else len(snippet)]
        for idx, match in enumerate(headers)
        if match.group("section_type") == "файл"
    ]


def _build_log_error_document(
    failure: FailedTestSummary,
    *,
    head_lines: int,
    tail_lines: int,
) -> tuple[str, frozenset[str]]:
    """Документ ошибок лога и ключи ошибок — для gate по логу.

    Документ — строки событий-ошибок без кадров стека, без времени, ID, имён потоков и
    цифр: номера строк, потоков и заказов — не другая проблема. Ключи — корневой класс
    исключения каждого блока (последний в блоке, то есть самый глубокий ``Caused by``;
    обёртки не считаются) и коды ошибок (``numeric_codes``): общий ключ у двух логов —
    та же ошибка, даже если вокруг неё разный шум.
    """
    if not failure.log_snippet:
        return "", frozenset()
    snippet = strip_source_marks(failure.log_snippet)
    if failure.log_selection_truncated:
        snippet = strip_log_selection_metadata(snippet)
    lines: list[str] = []
    keys: set[str] = set()
    for body in _file_sections(snippet):
        # Блоки событий разделены пустыми строками (render_error_blocks).
        for block in re.split(r"\n[ \t]*\n", body):
            block_lines = [line for line in block.splitlines()
                           if line.strip() and not _STACK_FRAME_RE.match(line)]
            classes = _EXCEPTION_CLASS_RE.findall("\n".join(block_lines))
            if classes:
                keys.add(classes[-1])
            lines.extend(block_lines)
    text = "\n".join(lines)
    keys.update(numeric_codes(text))
    compacted = _compact_trace(text, head_lines=head_lines, tail_lines=tail_lines)
    document = _DIGITS_RE.sub("#", normalize_text(replace_thread_names(compacted)))
    return (document if _TOKEN_RE.search(document) else ""), frozenset(keys)


def _get_failure_correlation(failure: FailedTestSummary) -> str | None:
    """Вернуть одну опорную correlation-строку для конкретного падения."""
    if failure.correlation_hint:
        return failure.correlation_hint
    return extract_correlation_from_log(failure.log_snippet)


def _select_cluster_correlation(
    representative: FailedTestSummary,
    group_failures: list[FailedTestSummary],
    member_ids: list[int],
) -> tuple[str, int] | None:
    """Выбрать одну correlation-строку на кластер вместе с тестом-источником.

    Приоритет:
    1. representative test
    2. остальные members по возрастанию test_result_id
    """
    representative_correlation = _get_failure_correlation(representative)
    if representative_correlation is not None:
        return representative_correlation, representative.test_result_id

    failures_by_id = {failure.test_result_id: failure for failure in group_failures}
    for test_id in member_ids:
        if test_id == representative.test_result_id:
            continue
        failure = failures_by_id[test_id]
        correlation = _get_failure_correlation(failure)
        if correlation is not None:
            return correlation, failure.test_result_id
    return None


# ---------------------------------------------------------------------------
# Сервис ClusteringService
# ---------------------------------------------------------------------------

class ClusteringService:
    """Группирует похожие ошибки тестов в кластеры по корневой причине.

    Алгоритм: message-first трёхканальный TF-IDF + agglomerative clustering
    (complete linkage). Каналы: message, trace, log (лог участвует когда
    доступен). Универсальный — работает с любым языком и форматом.
    """

    def __init__(self, config: ClusteringConfig | None = None) -> None:
        self._config = config or ClusteringConfig()

    def cluster_failures(
        self,
        launch_id: int,
        failures: list[FailedTestSummary],
    ) -> ClusteringReport:
        """Кластеризовать список ошибок и вернуть ``ClusteringReport``."""
        self._last_condensed: np.ndarray | None = None
        if not failures:
            return ClusteringReport(
                launch_id=launch_id,
                total_failures=0,
                cluster_count=0,
            )

        # 1. Собрать документы по отдельным каналам
        message_documents: list[str] = [_build_message_document(f) for f in failures]
        trace_documents: list[str] = [
            _build_trace_document(
                f,
                head_lines=self._config.trace_compact_head_lines,
                tail_lines=self._config.trace_compact_tail_lines,
            )
            for f in failures
        ]

        log_documents: list[str] = [
            _build_log_document(
                f,
                head_lines=self._config.log_compact_head_lines,
                tail_lines=self._config.log_compact_tail_lines,
            )
            for f in failures
        ]
        log_errors = [
            _build_log_error_document(
                f,
                head_lines=self._config.log_compact_head_lines,
                tail_lines=self._config.log_compact_tail_lines,
            )
            for f in failures
        ]
        step_documents: list[str] = [_build_step_document(f) for f in failures]
        assertion_actuals: list[str | None] = [
            _extract_assertion_actual(f.status_message or "") for f in failures
        ]

        # 2. Разделить на тесты с текстом и без (в любом канале)
        # Лог считается «текстом» только когда log_weight > 0: при явном opt-out
        # (ALLURE_LOGS_CLUSTERING_WEIGHT=0) тест только с log становится singleton.
        log_weight_positive = self._config.log_similarity_weight > 0
        has_text_indices: list[int] = []
        empty_indices: list[int] = []
        for i, (message_doc, trace_doc) in enumerate(
            zip(message_documents, trace_documents)
        ):
            has_log = log_weight_positive and bool(log_documents[i].strip())
            if message_doc.strip() or trace_doc.strip() or has_log:
                has_text_indices.append(i)
            else:
                empty_indices.append(i)

        # 3. Кластеризация тестов с текстом
        cluster_groups: dict[int, list[int]] = {}  # label -> индексы падений

        if len(has_text_indices) == 0:
            pass
        elif len(has_text_indices) == 1:
            cluster_groups[0] = [has_text_indices[0]]
        else:
            message_docs = [message_documents[i] for i in has_text_indices]
            trace_docs = [trace_documents[i] for i in has_text_indices]
            log_docs = [log_documents[i] for i in has_text_indices]
            step_docs = [step_documents[i] for i in has_text_indices]
            actuals = [assertion_actuals[i] for i in has_text_indices]
            errors = [log_errors[i] for i in has_text_indices]
            labels = self._cluster_texts(
                message_docs, trace_docs, log_docs if log_weight_positive else None, step_docs,
                assertion_actuals=actuals, log_errors=errors,
            )

            for idx, label in zip(has_text_indices, labels):
                cluster_groups.setdefault(label, []).append(idx)

        distance = _Distances(has_text_indices, self._last_condensed)
        documents = (message_documents, step_documents, log_documents)

        # 4. Конвертация в выходные модели
        result_clusters: list[FailureCluster] = []

        for group_indices in cluster_groups.values():
            cluster = self._build_cluster(group_indices, failures)
            cluster.examples = select_examples(group_indices, failures, distance, documents)
            result_clusters.append(cluster)

        # Singleton-кластеры — тесты без текста
        for idx in empty_indices:
            cluster = self._build_cluster([idx], failures)
            cluster.examples = select_examples([idx], failures, distance, documents)
            result_clusters.append(cluster)

        # Сортировка: самые крупные кластеры первыми, при равенстве — по ID
        result_clusters.sort(key=lambda c: (-c.member_count, c.cluster_id))

        unclustered = sum(1 for c in result_clusters if c.member_count == 1)

        logger.info(
            "Сгруппировано %d падений в %d кластеров (%d одиночных)",
            len(failures),
            len(result_clusters),
            unclustered,
        )

        return ClusteringReport(
            launch_id=launch_id,
            total_failures=len(failures),
            cluster_count=len(result_clusters),
            clusters=result_clusters,
            unclustered_count=unclustered,
        )

    # --- Кластеризация ---

    def _cluster_texts(
        self,
        message_documents: list[str],
        trace_documents: list[str],
        log_documents: list[str] | None = None,
        step_documents: list[str] | None = None,
        *,
        assertion_actuals: list[str | None] | None = None,
        log_errors: list[tuple[str, frozenset[str]]] | None = None,
    ) -> list[int]:
        """Message-first TF-IDF + агломеративная кластеризация.

        Возвращает список меток кластеров (одна метка на документ).
        """
        n = len(message_documents)
        message_sim = self._pairwise_similarity(message_documents)
        trace_sim = self._pairwise_similarity(trace_documents)

        log_sim: np.ndarray | None = None
        if log_documents and self._config.log_similarity_weight > 0:
            log_sim = self._pairwise_similarity(log_documents)
        # Документы ошибок лога — без обрезки словаря: у одинаковых документов, все слова
        # которых выпали бы из max_features, cosine был бы 0, и gate разделил бы их.
        error_sim: np.ndarray | None = None
        error_keys: list[frozenset[str]] = []
        has_errors = [False] * n
        if (log_errors is not None and log_sim is not None
                and self._config.log_split_threshold > 0):
            error_docs = [doc for doc, _ in log_errors]
            error_sim = self._pairwise_similarity(error_docs, capped=False)
            error_keys = [keys for _, keys in log_errors]
            has_errors = [bool(doc) for doc in error_docs]
        step_sim: np.ndarray | None = None
        if step_documents:
            step_sim = self._pairwise_similarity(step_documents)

        message_weight = self._config.message_similarity_weight
        trace_weight = self._config.trace_similarity_weight
        log_weight = (
            self._config.log_similarity_weight if log_sim is not None else 0.0
        )
        weight_sum = message_weight + trace_weight + log_weight
        if weight_sum > 0:
            message_weight /= weight_sum
            trace_weight /= weight_sum
            log_weight /= weight_sum
        else:
            message_weight = 1.0
            trace_weight = 0.0
            log_weight = 0.0

        condensed = np.ones(n * (n - 1) // 2, dtype=np.float64)
        collect_stats = logger.isEnabledFor(logging.DEBUG)
        final_min, final_max, final_sum = 1.0, 0.0, 0.0
        has_message = [bool(doc.strip()) for doc in message_documents]
        has_trace = [bool(doc.strip()) for doc in trace_documents]
        has_log = (
            [bool(doc.strip()) for doc in log_documents]
            if log_documents and log_weight > 0
            else [False] * n
        )
        has_step = (
            [bool(doc.strip()) for doc in step_documents]
            if step_documents
            else [False] * n
        )

        idx = -1
        for i in range(n):
            for j in range(i + 1, n):
                # scipy's condensed order: (0,1), (0,2), ..., (1,2), ...
                idx += 1
                # Gate по actual-значению assertion: разные actual → разные корневые причины.
                if (
                    assertion_actuals is not None
                    and assertion_actuals[i] is not None
                    and assertion_actuals[j] is not None
                    and assertion_actuals[i] != assertion_actuals[j]
                ):
                    if collect_stats:
                        final_min = 0.0
                    continue

                # Hard gate по step path: если у обоих failures есть непустой
                # нормализованный step path и их TF-IDF similarity ниже strict
                # threshold — пара принудительно не сливается. Применяется ДО
                # message/log логики, поэтому log override его не обходит.
                # Если у одного из failures step path отсутствует, gate не
                # применяется (не плодим мусорные «unknown step» кластера —
                # тогда работает только мягкий штраф ниже). Если разделение
                # оказалось ошибочным, объединить можно через merge rule с
                # `rule_kind="step"`.
                if (
                    step_sim is not None
                    and has_step[i]
                    and has_step[j]
                    and float(step_sim[i, j]) < self._config.step_path_strict_threshold
                ):
                    if collect_stats:
                        final_min = 0.0
                    continue

                # Gate по логу: у обоих в логе есть ошибки, общего ключа ошибки нет и
                # документы ошибок почти не похожи — разные ошибки сервиса за одинаковым
                # симптомом (пул БД и NPE за одним 500). Явный отказ от лога
                # (ALLURE_LOGS_CLUSTERING_WEIGHT=0) выключает и его.
                if (
                    error_sim is not None
                    and has_errors[i]
                    and has_errors[j]
                    and not error_keys[i] & error_keys[j]
                    and float(error_sim[i, j]) < self._config.log_split_threshold
                ):
                    if collect_stats:
                        final_min = 0.0
                    continue

                if has_message[i] and has_message[j]:
                    # Override по log: если лог-кластеризация включена (weight > 0),
                    # оба теста имеют лог и лог-similarity выше порога —
                    # обойти gate по message. Одинаковый application log при
                    # разных assertion → скорее всего одна проблема.
                    # Если log_weight == 0 (ALLURE_LOGS_CLUSTERING_WEIGHT=0),
                    # override не срабатывает — явный opt-out уважается.
                    log_overrides_gate = (
                        log_weight > 0
                        and log_sim is not None
                        and has_log[i]
                        and has_log[j]
                        and log_sim[i, j] >= self._config.similarity_threshold
                    )
                    if (
                        message_sim[i, j] < self._config.similarity_threshold
                        and not log_overrides_gate
                    ):
                        # Gate по message: если сообщения различаются ниже порога
                        # и лог не override'ит — пара не может быть склеена.
                        pair_sim = message_sim[i, j]
                    elif log_overrides_gate and message_sim[i, j] < self._config.similarity_threshold:
                        # Override по log: message различаются, но лог одинаковый.
                        # Лог становится доминирующим каналом (0.6 log + 0.2 msg + 0.2 trace).
                        assert log_sim is not None
                        log_pair_sim = float(log_sim[i, j])
                        pair_sim = 0.6 * log_pair_sim + 0.2 * message_sim[i, j]
                        if has_trace[i] and has_trace[j]:
                            pair_sim += 0.2 * trace_sim[i, j]
                        else:
                            pair_sim += 0.2 * message_sim[i, j]
                    elif not (has_trace[i] and has_trace[j]) and not (has_log[i] and has_log[j]):
                        # Нет дополнительных каналов — только message.
                        pair_sim = message_sim[i, j]
                    else:
                        pair_sim = message_weight * message_sim[i, j]
                        if has_trace[i] and has_trace[j]:
                            pair_sim += trace_weight * trace_sim[i, j]
                        else:
                            # Перераспределить вес trace на message
                            pair_sim += trace_weight * message_sim[i, j]
                        if log_sim is not None and has_log[i] and has_log[j]:
                            pair_sim += log_weight * log_sim[i, j]
                        else:
                            # Перераспределить вес log на message
                            pair_sim += log_weight * message_sim[i, j]
                else:
                    # Нет message — fallback на trace (+ log если есть и weight > 0)
                    pair_sim = trace_sim[i, j]
                    if log_weight > 0 and log_sim is not None and has_log[i] and has_log[j]:
                        # Смешать trace и log когда нет message
                        if has_trace[i] and has_trace[j]:
                            tw = trace_weight / (trace_weight + log_weight) if (trace_weight + log_weight) > 0 else 1.0
                            lw = 1.0 - tw
                            pair_sim = tw * trace_sim[i, j] + lw * log_sim[i, j]
                        else:
                            pair_sim = log_sim[i, j]

                if step_sim is not None and has_step[i] and has_step[j]:
                    step_penalty = self._config.step_path_mismatch_penalty
                    if log_weight > 0 and has_log[i] and has_log[j]:
                        step_penalty *= self._config.step_path_log_reduction
                    pair_sim = max(
                        0.0,
                        pair_sim - step_penalty * (1.0 - float(step_sim[i, j])),
                    )

                condensed[idx] = 1.0 - pair_sim
                if collect_stats:
                    final_min = min(final_min, float(pair_sim))
                    final_max = max(final_max, float(pair_sim))
                    final_sum += float(pair_sim)

        if collect_stats:
            final_stats = (final_min, final_sum / len(condensed), final_max)
            self._log_similarity_stats(message_sim, trace_sim, final_stats)
        np.clip(condensed, 0.0, 1.0, out=condensed)
        # Итоговые попарные расстояния нужны выбору примеров кластера (медоид и т. п.).
        self._last_condensed = condensed

        # Агломеративная кластеризация (complete linkage)
        linkage_matrix = linkage(condensed, method="complete")
        labels = fcluster(
            linkage_matrix,
            t=self._config.distance_threshold,
            criterion="distance",
        )

        return [int(label) for label in labels.tolist()]

    def _pairwise_similarity(self, documents: list[str], *, capped: bool = True) -> np.ndarray:
        """Матрица cosine similarity по списку документов.

        ``capped=False`` — без ограничения словаря ``tfidf_max_features``.
        Пустые документы не участвуют в векторизации и имеют similarity=0
        с любыми другими документами (кроме диагонали=1).
        """
        n = len(documents)
        non_empty_indices = [i for i, doc in enumerate(documents) if doc.strip()]

        if len(non_empty_indices) <= 1:
            return np.eye(n, dtype=np.float64)

        vectorizer = TfidfVectorizer(
            max_features=self._config.tfidf_max_features if capped else None,
            ngram_range=self._config.tfidf_ngram_range,
            token_pattern=r"(?u)\b\w\w+\b",
            lowercase=True,
        )
        subset_docs = [documents[i] for i in non_empty_indices]
        try:
            tfidf_matrix = vectorizer.fit_transform(subset_docs)
        except ValueError:
            return np.eye(n, dtype=np.float64)

        subset_sim: np.ndarray = cosine_similarity(tfidf_matrix)
        np.clip(subset_sim, 0.0, 1.0, out=subset_sim)
        np.fill_diagonal(subset_sim, 1.0)
        if len(non_empty_indices) == n:
            return subset_sim
        sim_matrix = np.eye(n, dtype=np.float64)
        sim_matrix[np.ix_(non_empty_indices, non_empty_indices)] = subset_sim
        return sim_matrix

    @staticmethod
    def _similarity_stats(matrix: np.ndarray) -> tuple[float, float, float]:
        """Вернуть min/avg/max по попарным similarity без диагонали."""
        if matrix.shape[0] < 2:
            return 1.0, 1.0, 1.0

        values = matrix[np.triu_indices(matrix.shape[0], k=1)]
        if values.size == 0:
            return 1.0, 1.0, 1.0

        return float(values.min()), float(values.mean()), float(values.max())

    def _log_similarity_stats(
        self,
        message_sim: np.ndarray,
        trace_sim: np.ndarray,
        final_stats: tuple[float, float, float],
    ) -> None:
        """DEBUG-лог статистики similarity матриц для диагностики кластеризации."""
        if not logger.isEnabledFor(logging.DEBUG):
            return

        msg_min, msg_avg, msg_max = self._similarity_stats(message_sim)
        trace_min, trace_avg, trace_max = self._similarity_stats(trace_sim)
        final_min, final_avg, final_max = final_stats
        logger.debug(
            "Similarity stats: "
            "message(min=%.4f avg=%.4f max=%.4f), "
            "trace(min=%.4f avg=%.4f max=%.4f), "
            "final(min=%.4f avg=%.4f max=%.4f)",
            msg_min,
            msg_avg,
            msg_max,
            trace_min,
            trace_avg,
            trace_max,
            final_min,
            final_avg,
            final_max,
        )

    # --- Построение кластера ---

    def _build_cluster(
        self,
        indices: list[int],
        failures: list[FailedTestSummary],
    ) -> FailureCluster:
        """Создать FailureCluster из группы индексов."""
        group_failures = [failures[i] for i in indices]

        # Представитель: тест с самым длинным message, при равенстве — меньший ID
        representative = max(
            group_failures,
            key=lambda f: (len(f.status_message or ""), -f.test_result_id),
        )

        member_ids = sorted(f.test_result_id for f in group_failures)

        # Сигнатура — для совместимости с существующей моделью
        signature = ClusterSignature(
            message_pattern=(
                representative.status_message[:100]
                if representative.status_message
                else None
            ),
            category=representative.category,
        )

        label = self._generate_label(representative)
        correlation_selection = _select_cluster_correlation(
            representative,
            group_failures,
            member_ids,
        )
        example_correlation: str | None = None
        example_correlation_test_id: int | None = None
        if correlation_selection is not None:
            example_correlation, example_correlation_test_id = correlation_selection

        return FailureCluster(
            cluster_id=self._generate_cluster_id(signature, member_ids),
            label=label,
            signature=signature,
            member_test_ids=member_ids,
            member_count=len(member_ids),
            representative_test_id=representative.test_result_id,
            example_message=representative.status_message,
            example_trace_snippet=_first_n_lines(
                representative.status_trace,
                self._config.trace_snippet_lines,
            ),
            example_step_path=representative.failed_step_path,
            example_correlation=example_correlation,
            example_correlation_test_id=example_correlation_test_id,
        )

    def _generate_label(self, representative: FailedTestSummary) -> str:
        """Сгенерировать метку кластера из представителя.

        Просто показываем текст ошибки — без парсинга exception type.
        """
        if representative.status_message:
            msg = representative.status_message.strip()
            if len(msg) > self._config.max_label_length:
                return msg[: self._config.max_label_length - 3] + "..."
            return msg

        if representative.status_trace:
            first_line = representative.status_trace.strip().split("\n", 1)[0]
            if len(first_line) > self._config.max_label_length:
                return first_line[: self._config.max_label_length - 3] + "..."
            return first_line

        if representative.category:
            return f"Категория: {representative.category}"

        # Имя, а не ID результата: метка попадает в сводку и отчёт, «Тест: 301» читателю
        # ничего не говорит (стенд Qwen, E03).
        return f"Тест: {representative.name or representative.test_result_id}"

    @staticmethod
    def _generate_cluster_id(
        signature: ClusterSignature,
        member_ids: list[int] | None = None,
    ) -> str:
        return generate_cluster_id(signature, member_ids)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def generate_cluster_id(
    signature: ClusterSignature,
    member_ids: list[int] | None = None,
) -> str:
    """Детерминированный ID кластера на основе SHA-256 хеша сигнатуры.

    member_ids всегда включаются в хеш для гарантии уникальности,
    даже при совпадающих сигнатурах.
    """
    components = [
        signature.exception_type or "",
        signature.category or "",
        "|".join(signature.common_frames),
        signature.message_pattern or "",
    ]
    if member_ids:
        components.append("|".join(str(tid) for tid in sorted(member_ids)))
    raw = "\n".join(components)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class _Distances:
    """Итоговое попарное расстояние двух падений из condensed-вектора кластеризации."""

    def __init__(self, indices: list[int], condensed: np.ndarray | None) -> None:
        self._position = {index: position for position, index in enumerate(indices)}
        self._n = len(indices)
        self._condensed = condensed

    def __call__(self, a: int, b: int) -> float:
        if a == b:
            return 0.0
        if self._condensed is None or a not in self._position or b not in self._position:
            return 1.0
        i, j = sorted((self._position[a], self._position[b]))
        return float(self._condensed[self._n * i - i * (i + 1) // 2 + (j - i - 1)])


_DIGITS_RE = re.compile(r"\d+")
# HTTP-статус в логе: «HTTP 401», «HTTP/1.1 403», «HTTP статус: 401» (так пишет сам
# извлекатель), «status=401», «status="401"», «"status":401», «statusCode: 401»,
# «response_code=401», «POST /orders -> 401». За числом не должно идти буквы или дробной
# части: «response 401ms», «took 401.5s» — длительность, а не статус.
_HTTP_STATUS_RE = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?(?:\s+(?:статус|status))?|статус|"
    r"(?:http[ _]?)?status(?:[ _]?code)?|response[ _]?code|code|returned|response)"
    r"[\"']?\s*[:=]?\s*[\"']?([1-5]\d\d)(?![\w]|\.\d)|->\s*([1-5]\d\d)(?![\w]|\.\d)",
    re.IGNORECASE,
)


def _log_codes(failure: FailedTestSummary) -> tuple[str, ...]:
    """Коды ошибок из исходного лога: нормализованный уже заменил длинные числа на <NUM>."""
    raw = strip_source_marks(failure.log_snippet or "")
    http = {"http=" + (a or b) for a, b in _HTTP_STATUS_RE.findall(raw)}
    return tuple(sorted(set(numeric_codes(raw)) | http))


def _error_events(failure: FailedTestSummary) -> int:
    """Сколько событий-ошибок в логе теста: по пометкам строк источника (шаг 2)."""
    return len(SOURCE_MARK_RE.findall(failure.log_snippet or ""))


def select_examples(
    indices: list[int],
    failures: list[FailedTestSummary],
    distance: Callable[[int, int], float],
    documents: tuple[list[str], list[str], list[str]],
) -> list[ClusterExample]:
    """До трёх примеров кластера, детерминированно; ничья — меньший id теста.

    * ``typical`` — медоид: минимальная сумма расстояний до остальных участников;
    * ``different`` — самый далёкий от типичного, если его сообщение, шаг или лог
      отличаются после нормализации (логи — когда есть у обоих: нет лога — не отличие);
    * ``informative`` — больше всего событий-ошибок в логе среди остальных, если его
      лог отличается от логов уже выбранных.

    Кластер из одного теста или без различий — один пример.
    """
    def test_id(index: int) -> int:
        return failures[index].test_result_id

    messages, steps, raw_logs = documents
    # Номера строк, потоков, коротких id в логе (exec-1 / exec-7, Worker.java:40 / :41) — не
    # другая проблема: логи сравниваются без цифр, но с кодами ошибок (error_code=10001,
    # ORA-01017, HTTP 401) из исходного лога — разные коды остаются разными ошибками.
    logs = [(_DIGITS_RE.sub("#", log), _log_codes(failures[index]))
            for index, log in enumerate(raw_logs)]
    typical = min(indices, key=lambda i: (sum(distance(i, j) for j in indices), test_id(i)))
    chosen = [typical]
    examples = [ClusterExample(role="typical", test_result_id=test_id(typical))]

    def differs(a: int, b: int) -> bool:
        # Нет лога у одного из тестов — это не другая проблема: логи сравниваются, только
        # когда есть у обоих.
        both_logs = bool(raw_logs[a].strip()) and bool(raw_logs[b].strip())
        return (messages[a] != messages[b] or steps[a] != steps[b]
                or (both_logs and logs[a] != logs[b]))

    rest = [i for i in indices if i != typical]
    if rest:
        farthest = min(rest, key=lambda i: (-distance(typical, i), test_id(i)))
        if differs(farthest, typical):
            chosen.append(farthest)
            examples.append(ClusterExample(role="different", test_result_id=test_id(farthest)))
    # Повторы уже выбранных логов исключаются до выбора: иначе дубликат с наибольшим числом
    # ошибок закрывал бы следующий, действительно другой лог.
    others = [i for i in indices if i not in chosen and _error_events(failures[i]) > 0
              and all(logs[i] != logs[j] for j in chosen)]
    if others:
        richest = min(others, key=lambda i: (-_error_events(failures[i]), test_id(i)))
        examples.append(ClusterExample(role="informative", test_result_id=test_id(richest)))
    return examples


def _first_n_lines(text: str | None, n: int) -> str | None:
    """Вернуть первые n строк текста или None."""
    if not text:
        return None
    lines = text.strip().splitlines()[:n]
    return "\n".join(lines)
