"""Сервис кластеризации похожих ошибок тестов по корневой причине.

Алгоритм: message-first подход.
1. Для каждого падения строятся два канала текста:
   - message-document: status_message + category
   - trace-document: status_trace (с компактацией длинных трасс)
2. Минимальная нормализация: замена волатильных данных (UUID, timestamps,
   длинные числа, IP) плейсхолдерами.
3. TF-IDF + cosine similarity по каждому каналу отдельно.
4. Итоговая similarity для пары:
   - если message есть у обоих и message similarity ниже порога,
     trace игнорируется (пара не может быть склеена)
   - иначе взвешенная комбинация message/trace
   - если message у одного/обоих пустой, fallback только на trace
5. Agglomerative clustering (complete linkage) по итоговой distance.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from alla.models.clustering import (
    ClusteringReport,
    ClusterSignature,
    FailureCluster,
)
from alla.models.testops import FailedTestSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
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

    @property
    def distance_threshold(self) -> float:
        """Перевод similarity_threshold в distance_threshold для scipy."""
        return 1.0 - self.similarity_threshold


# ---------------------------------------------------------------------------
# Text normalization — замена волатильных данных
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_UUID_NOHYPHEN_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)

# --- Даты и время (от более специфичных к менее специфичным) ---

# ISO 8601 полный datetime + опциональные секунды, millis/micros и timezone.
# Ловит HH:MM и HH:MM:SS, а также Java/Log4j запятую: 2026-02-06 10:12:13,123
_DATETIME_ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2})?"
    r"(?:[.,]\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)

# Именованные месяцы (EN): "Feb 6, 2026", "06 Feb 2026", "6-Feb-2026"
# + опциональное время после даты.
_MONTH_NAMES = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATETIME_NAMED_MONTH_RE = re.compile(
    r"(?:"
    r"\d{1,2}[- ]" + _MONTH_NAMES + r"[- ]\d{4}"
    r"|"
    + _MONTH_NAMES + r"\.?\s+\d{1,2},?\s+\d{4}"
    r")"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)?",
    re.IGNORECASE,
)

# Слэш-даты: 02/06/2026, 2026/02/06 (требуется 4-значный год)
_DATE_SLASH_RE = re.compile(
    r"\b\d{4}/\d{1,2}/\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
)

# Точка-даты: 06.02.2026, 2026.02.06 (требуется 4-значный год → не ловит версии)
_DATE_DOT_RE = re.compile(
    r"\b\d{4}\.\d{1,2}\.\d{1,2}\b"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
)

# ISO дата без времени: 2026-02-06
# Lookahead: не совпадать, если далее идёт компонент времени (T12:34:56 или " 12:34:56"
# либо HH:MM без секунд). _DATETIME_ISO_RE уже обработал полные datetime, здесь остаток.
_DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b(?![T ]\d{2}:\d{2}:\d{2})")

# Standalone время: 10:12:13, 10:12:13.123, 10:12:13,456
_TIME_ONLY_RE = re.compile(
    r"(?<!\d[.:])\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
)

_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def _normalize_text(text: str) -> str:
    """Заменить волатильные данные плейсхолдерами.

    Не трогаем саму структуру текста, не удаляем стоп-слова,
    не приводим к lowercase — всё это делает TfidfVectorizer.

    Порядок применения критичен:
    - UUID до дат (hex-UUID содержит цифры, похожие на даты)
    - Полный datetime до date-only (иначе дата матчится отдельно от времени)
    - IP до точка-дат (192.168.1.1 не должен стать <TS>)
    - Long numbers последними (иначе год «2026» станет <NUM> до матча даты)
    """
    text = _UUID_RE.sub("<ID>", text)
    text = _UUID_NOHYPHEN_RE.sub("<ID>", text)
    text = _DATETIME_ISO_RE.sub("<TS>", text)
    text = _DATETIME_NAMED_MONTH_RE.sub("<TS>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _DATE_SLASH_RE.sub("<TS>", text)
    text = _DATE_DOT_RE.sub("<TS>", text)
    text = _DATE_ISO_RE.sub("<TS>", text)
    text = _TIME_ONLY_RE.sub("<TS>", text)
    text = _LONG_NUMBER_RE.sub("<NUM>", text)
    return text


def _build_message_document(failure: FailedTestSummary) -> str:
    """Собрать message-документ (message + category)."""
    parts: list[str] = []

    if failure.status_message:
        parts.append(failure.status_message)

    if failure.category:
        parts.append(failure.category)

    raw = "\n".join(parts)
    return _normalize_text(raw) if raw else ""


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
    return _normalize_text(compacted) if compacted else ""


def _build_log_document(
    failure: FailedTestSummary,
    *,
    head_lines: int,
    tail_lines: int,
) -> str:
    """Собрать log-документ из log_snippet с предварительной компактацией."""
    if not failure.log_snippet:
        return ""
    compacted = _compact_trace(
        failure.log_snippet,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )
    return _normalize_text(compacted) if compacted else ""


# ---------------------------------------------------------------------------
# ClusteringService
# ---------------------------------------------------------------------------

class ClusteringService:
    """Группирует похожие ошибки тестов в кластеры по корневой причине.

    Алгоритм: message-first двухканальный TF-IDF + agglomerative clustering
    (complete linkage). Универсальный — работает с любым языком и форматом.
    """

    def __init__(self, config: ClusteringConfig | None = None) -> None:
        self._config = config or ClusteringConfig()

    def cluster_failures(
        self,
        launch_id: int,
        failures: list[FailedTestSummary],
    ) -> ClusteringReport:
        """Кластеризовать список ошибок и вернуть ``ClusteringReport``."""
        if not failures:
            return ClusteringReport(
                launch_id=launch_id,
                total_failures=0,
                cluster_count=0,
            )

        # 1. Собрать документы по двум (+опционально трём) каналам
        message_documents: list[str] = [_build_message_document(f) for f in failures]
        trace_documents: list[str] = [
            _build_trace_document(
                f,
                head_lines=self._config.trace_compact_head_lines,
                tail_lines=self._config.trace_compact_tail_lines,
            )
            for f in failures
        ]

        log_documents: list[str] | None = None
        if self._config.log_similarity_weight > 0:
            log_documents = [
                _build_log_document(
                    f,
                    head_lines=self._config.log_compact_head_lines,
                    tail_lines=self._config.log_compact_tail_lines,
                )
                for f in failures
            ]

        # 2. Разделить на тесты с текстом и без (в любом канале)
        has_text_indices: list[int] = []
        empty_indices: list[int] = []
        for i, (message_doc, trace_doc) in enumerate(
            zip(message_documents, trace_documents)
        ):
            has_log = (
                log_documents[i].strip() if log_documents else False
            )
            if message_doc.strip() or trace_doc.strip() or has_log:
                has_text_indices.append(i)
            else:
                empty_indices.append(i)

        # 3. Кластеризация тестов с текстом
        cluster_groups: dict[int, list[int]] = {}  # label -> [failure indices]

        if len(has_text_indices) == 0:
            pass
        elif len(has_text_indices) == 1:
            cluster_groups[0] = [has_text_indices[0]]
        else:
            message_docs = [message_documents[i] for i in has_text_indices]
            trace_docs = [trace_documents[i] for i in has_text_indices]
            log_docs = (
                [log_documents[i] for i in has_text_indices]
                if log_documents
                else None
            )
            labels = self._cluster_texts(message_docs, trace_docs, log_docs)

            for idx, label in zip(has_text_indices, labels):
                cluster_groups.setdefault(label, []).append(idx)

        # 4. Конвертация в выходные модели
        result_clusters: list[FailureCluster] = []

        for group_indices in cluster_groups.values():
            cluster = self._build_cluster(group_indices, failures)
            result_clusters.append(cluster)

        # Singletons — тесты без текста
        for idx in empty_indices:
            cluster = self._build_cluster([idx], failures)
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

    # --- Clustering ---

    def _cluster_texts(
        self,
        message_documents: list[str],
        trace_documents: list[str],
        log_documents: list[str] | None = None,
    ) -> list[int]:
        """Message-first TF-IDF + agglomerative clustering.

        Возвращает список меток кластеров (одна метка на документ).
        """
        n = len(message_documents)
        message_sim = self._pairwise_similarity(message_documents)
        trace_sim = self._pairwise_similarity(trace_documents)

        log_sim: np.ndarray | None = None
        if log_documents and self._config.log_similarity_weight > 0:
            log_sim = self._pairwise_similarity(log_documents)

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

        final_sim = np.eye(n, dtype=np.float64)
        has_message = [bool(doc.strip()) for doc in message_documents]
        has_trace = [bool(doc.strip()) for doc in trace_documents]
        has_log = (
            [bool(doc.strip()) for doc in log_documents]
            if log_documents
            else [False] * n
        )

        for i in range(n):
            for j in range(i + 1, n):
                if has_message[i] and has_message[j]:
                    if message_sim[i, j] < self._config.similarity_threshold:
                        # Message gate: если сообщения различаются ниже порога,
                        # trace/log не могут "перетащить" пару в один кластер.
                        pair_sim = message_sim[i, j]
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
                    # Нет message — fallback на trace (+ log если есть)
                    pair_sim = trace_sim[i, j]
                    if log_sim is not None and has_log[i] and has_log[j]:
                        # Смешать trace и log когда нет message
                        if has_trace[i] and has_trace[j]:
                            tw = trace_weight / (trace_weight + log_weight) if (trace_weight + log_weight) > 0 else 1.0
                            lw = 1.0 - tw
                            pair_sim = tw * trace_sim[i, j] + lw * log_sim[i, j]
                        else:
                            pair_sim = log_sim[i, j]

                final_sim[i, j] = pair_sim
                final_sim[j, i] = pair_sim

        self._log_similarity_stats(message_sim, trace_sim, final_sim)

        np.clip(final_sim, 0.0, 1.0, out=final_sim)
        dist_matrix = 1.0 - final_sim

        # Condensed form для scipy
        condensed = np.zeros(n * (n - 1) // 2, dtype=np.float64)
        idx = 0
        for i in range(n):
            for j in range(i + 1, n):
                condensed[idx] = dist_matrix[i, j]
                idx += 1

        # Agglomerative clustering (complete linkage)
        linkage_matrix = linkage(condensed, method="complete")
        labels = fcluster(
            linkage_matrix,
            t=self._config.distance_threshold,
            criterion="distance",
        )

        return labels.tolist()

    def _pairwise_similarity(self, documents: list[str]) -> np.ndarray:
        """Cosine similarity matrix по списку документов.

        Пустые документы не участвуют в векторизации и имеют similarity=0
        с любыми другими документами (кроме диагонали=1).
        """
        n = len(documents)
        sim_matrix = np.eye(n, dtype=np.float64)
        non_empty_indices = [i for i, doc in enumerate(documents) if doc.strip()]

        if len(non_empty_indices) <= 1:
            return sim_matrix

        vectorizer = TfidfVectorizer(
            max_features=self._config.tfidf_max_features,
            ngram_range=self._config.tfidf_ngram_range,
            token_pattern=r"(?u)\b\w\w+\b",
            lowercase=True,
        )
        subset_docs = [documents[i] for i in non_empty_indices]
        try:
            tfidf_matrix = vectorizer.fit_transform(subset_docs)
        except ValueError:
            return sim_matrix

        subset_sim = cosine_similarity(tfidf_matrix)
        np.clip(subset_sim, 0.0, 1.0, out=subset_sim)
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
        final_sim: np.ndarray,
    ) -> None:
        """DEBUG-лог статистики similarity матриц для диагностики кластеризации."""
        if not logger.isEnabledFor(logging.DEBUG):
            return

        msg_min, msg_avg, msg_max = self._similarity_stats(message_sim)
        trace_min, trace_avg, trace_max = self._similarity_stats(trace_sim)
        final_min, final_avg, final_max = self._similarity_stats(final_sim)
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

    # --- Cluster building ---

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

        return f"Тест: {representative.test_result_id}"

    @staticmethod
    def _generate_cluster_id(
        signature: ClusterSignature,
        member_ids: list[int] | None = None,
    ) -> str:
        """Детерминированный ID кластера на основе SHA-256 хеша сигнатуры.

        Для кластеров с пустой сигнатурой (нет message/trace/category)
        member_ids включаются в хеш для гарантии уникальности.
        """
        components = [
            signature.exception_type or "",
            signature.category or "",
            "|".join(signature.common_frames),
            signature.message_pattern or "",
        ]
        has_content = any(components)
        if not has_content and member_ids:
            components.append("|".join(str(tid) for tid in sorted(member_ids)))
        raw = "\n".join(components)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_n_lines(text: str | None, n: int) -> str | None:
    """Вернуть первые n строк текста или None."""
    if not text:
        return None
    lines = text.strip().splitlines()[:n]
    return "\n".join(lines)
