"""Сервис кластеризации похожих ошибок тестов по корневой причине.

Алгоритм: agglomerative clustering (complete linkage) по композитной
multi-signal метрике расстояния. TF-IDF + cosine для сообщений об ошибках,
set Jaccard для стек-трейс фреймов, exact match для типа исключения и категории.

Отсутствующие сигналы исключаются из сравнения, а их вес перераспределяется
между оставшимися. Если ВСЕ сигналы отсутствуют — расстояние = 1.0
(тесты не кластеризуются).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

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

    weight_exception_type: float = 0.35
    weight_message: float = 0.30
    weight_trace: float = 0.25
    weight_category: float = 0.10

    trace_root_frames: int = 8
    max_label_length: int = 120
    trace_snippet_lines: int = 5

    min_signal_count: int = 1

    tfidf_max_features: int = 500
    tfidf_min_df: int = 1
    tfidf_ngram_range: tuple[int, int] = (1, 2)

    ignored_frame_packages: tuple[str, ...] = (
        "java.lang.reflect",
        "java.lang.Thread",
        "sun.reflect",
        "jdk.internal",
        "org.springframework.cglib",
        "org.springframework.aop",
        "org.junit",
        "org.testng",
        "pytest",
        "unittest",
        "_pytest",
        "pluggy",
    )

    @property
    def distance_threshold(self) -> float:
        """Перевод similarity_threshold в distance_threshold для scipy."""
        return 1.0 - self.similarity_threshold


# ---------------------------------------------------------------------------
# FailureFeatures — extracted features for comparison
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureFeatures:
    """Извлечённые фичи одной ошибки для вычисления расстояний."""

    test_result_id: int
    exception_type: str | None
    normalized_message: str | None
    message_tokens: frozenset[str]
    trace_frame_set: frozenset[str]
    trace_frames_ordered: tuple[str, ...]
    category: str | None
    raw_message_prefix: str | None

    @property
    def signal_count(self) -> int:
        """Количество непустых сигналов."""
        count = 0
        if self.exception_type is not None:
            count += 1
        if self.normalized_message:
            count += 1
        if self.trace_frame_set:
            count += 1
        if self.category is not None:
            count += 1
        return count


# ---------------------------------------------------------------------------
# FeatureExtractor
# ---------------------------------------------------------------------------

_JAVA_EXCEPTION_RE = re.compile(
    r"(?:[\w.$]+\.)?(\w+(?:Exception|Error|Failure|Timeout))\b"
)
_PYTHON_EXCEPTION_RE = re.compile(
    r"^(\w+(?:Error|Exception|Warning|Failure))\s*:", re.MULTILINE
)
_HTTP_STATUS_ERROR_RE = re.compile(
    r"(?:got|received|returned|actual)\s*[:=]?\s*(\d{3})", re.IGNORECASE
)
_HTTP_STATUS_RE = re.compile(
    r"(?:status|code|response)\s*[:=]?\s*(\d{3})", re.IGNORECASE
)
_TIMEOUT_RE = re.compile(
    r"(timed?\s*out|timeout|connect(?:ion)?\s+refused)", re.IGNORECASE
)

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "was", "at", "in", "for", "to", "of",
    "and", "or", "but", "with", "not", "no", "be", "by", "on",
    "it", "that", "this", "from", "has", "had", "have",
})

_JAVA_FRAME_RE = re.compile(r"at\s+([\w.$]+)\(")
_PYTHON_FRAME_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+\d+,\s+in\s+(\w+)')


class FeatureExtractor:
    """Извлекает нормализованные фичи из сообщений об ошибках и стек-трейсов."""

    def __init__(self, config: ClusteringConfig) -> None:
        self._config = config

    def extract(self, failure: FailedTestSummary) -> FailureFeatures:
        """Извлечь ``FailureFeatures`` из ``FailedTestSummary``."""
        exception_type = self.extract_exception_type(
            failure.status_message, failure.status_trace,
        )

        normalized_msg = self._normalize_message_text(failure.status_message)
        message_tokens = self._tokenize(normalized_msg) if normalized_msg else frozenset()

        trace_frames_ordered = self.normalize_trace(failure.status_trace)
        trace_frame_set = frozenset(trace_frames_ordered)

        raw_prefix = failure.status_message[:100] if failure.status_message else None

        return FailureFeatures(
            test_result_id=failure.test_result_id,
            exception_type=exception_type,
            normalized_message=normalized_msg,
            message_tokens=message_tokens,
            trace_frame_set=trace_frame_set,
            trace_frames_ordered=trace_frames_ordered,
            category=failure.category,
            raw_message_prefix=raw_prefix,
        )

    def extract_exception_type(
        self,
        message: str | None,
        trace: str | None,
    ) -> str | None:
        """Определить тип исключения из сообщения или трейса."""
        sources = []
        if trace:
            sources.append(trace.strip().splitlines()[0] if trace.strip() else "")
        if message:
            sources.append(message)

        for text in sources:
            m = _JAVA_EXCEPTION_RE.search(text)
            if m:
                return m.group(1)

            m = _PYTHON_EXCEPTION_RE.search(text)
            if m:
                return m.group(1)

        combined = " ".join(s for s in sources if s)
        if _TIMEOUT_RE.search(combined):
            return "TimeoutError"

        m = _HTTP_STATUS_ERROR_RE.search(combined)
        if m:
            return f"HTTPStatus{m.group(1)}"
        m = _HTTP_STATUS_RE.search(combined)
        if m:
            return f"HTTPStatus{m.group(1)}"

        return None

    def _normalize_message_text(self, message: str | None) -> str | None:
        """Нормализовать сообщение об ошибке для TF-IDF: замена волатильных частей."""
        if not message:
            return None

        text = message.lower()
        text = _UUID_RE.sub("UUID", text)
        text = _TIMESTAMP_RE.sub("TIMESTAMP", text)
        text = _IP_RE.sub("IPADDR", text)
        text = _PATH_RE.sub("FILEPATH", text)
        text = _NUMBER_RE.sub("NUM", text)

        return text.strip() if text.strip() else None

    @staticmethod
    def _tokenize(text: str) -> frozenset[str]:
        """Извлечь набор токенов (для генерации меток кластеров)."""
        tokens = re.findall(r"[a-z_]+(?:\d+)?", text)
        return frozenset(
            t for t in tokens if t not in _STOP_WORDS and len(t) > 1
        )

    def normalize_trace(self, trace: str | None) -> tuple[str, ...]:
        """Извлечь и нормализовать N фреймов стек-трейса, ближайших к причине ошибки."""
        if not trace:
            return ()

        java_frames: list[str] = []
        python_frames: list[str] = []

        for line in trace.strip().splitlines():
            line = line.strip()

            m = _JAVA_FRAME_RE.search(line)
            if m:
                java_frames.append(m.group(1))
                continue

            m = _PYTHON_FRAME_RE.search(line)
            if m:
                python_frames.append(f"{m.group(1)}:{m.group(2)}")
                continue

        if python_frames and len(python_frames) >= len(java_frames):
            frames = python_frames
            is_python = True
        else:
            frames = java_frames
            is_python = False

        frames = [
            f for f in frames
            if not any(f.startswith(pkg) for pkg in self._config.ignored_frame_packages)
        ]

        root_count = self._config.trace_root_frames
        if is_python:
            root_frames = frames[-root_count:] if frames else []
        else:
            root_frames = frames[:root_count]

        return tuple(root_frames)


# ---------------------------------------------------------------------------
# ClusteringService
# ---------------------------------------------------------------------------

class ClusteringService:
    """Группирует похожие ошибки тестов в кластеры по корневой причине.

    Алгоритм: agglomerative clustering (complete linkage) по композитной
    multi-signal метрике расстояния. Complete linkage гарантирует, что
    КАЖДАЯ пара тестов внутри кластера имеет расстояние < порога.
    """

    def __init__(self, config: ClusteringConfig | None = None) -> None:
        self._config = config or ClusteringConfig()
        self._extractor = FeatureExtractor(self._config)

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

        # 1. Извлечение features для каждого теста
        features_list = [self._extractor.extract(f) for f in failures]
        failure_map = {f.test_result_id: f for f in failures}

        # 2. Разделение на clusterable и singletons
        clusterable: list[FailureFeatures] = []
        singleton_features: list[FailureFeatures] = []
        for feat in features_list:
            if feat.signal_count >= self._config.min_signal_count:
                clusterable.append(feat)
            else:
                singleton_features.append(feat)

        # Сортировка для детерминизма
        clusterable.sort(key=lambda f: f.test_result_id)
        singleton_features.sort(key=lambda f: f.test_result_id)

        # 3. Кластеризация clusterable элементов
        cluster_groups: dict[int, list[FailureFeatures]] = {}

        if len(clusterable) == 0:
            pass
        elif len(clusterable) == 1:
            cluster_groups[0] = [clusterable[0]]
        else:
            # 3a. TF-IDF для сообщений
            tfidf_matrix = self._build_tfidf_vectors(clusterable)

            # 3b. Cosine similarity matrix для сообщений
            message_sim_matrix = None
            if tfidf_matrix is not None:
                message_sim_matrix = cosine_similarity(tfidf_matrix)

            # 3c. Condensed distance matrix
            condensed_dist = self._build_condensed_distance_matrix(
                clusterable, message_sim_matrix,
            )

            # 3d. Agglomerative clustering
            linkage_matrix = linkage(condensed_dist, method="complete")
            labels = fcluster(
                linkage_matrix,
                t=self._config.distance_threshold,
                criterion="distance",
            ).tolist()

            for feat, label in zip(clusterable, labels):
                cluster_groups.setdefault(label, []).append(feat)

        # 4. Конвертация в выходные модели
        result_clusters: list[FailureCluster] = []

        for group_features in cluster_groups.values():
            cluster = self._build_cluster(group_features, failure_map)
            result_clusters.append(cluster)

        for feat in singleton_features:
            cluster = self._build_cluster([feat], failure_map)
            result_clusters.append(cluster)

        # Сортировка: самые крупные кластеры первыми
        result_clusters.sort(key=lambda c: (-c.member_count, c.cluster_id))

        unclustered = sum(1 for c in result_clusters if c.member_count == 1)

        logger.info(
            "Clustered %d failures into %d clusters (%d singletons)",
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

    # --- TF-IDF ---

    def _build_tfidf_vectors(
        self,
        features: list[FailureFeatures],
    ):
        """Построить TF-IDF матрицу для сообщений об ошибках."""
        messages = [f.normalized_message or "" for f in features]

        if not any(m.strip() for m in messages):
            return None

        vectorizer = TfidfVectorizer(
            max_features=self._config.tfidf_max_features,
            min_df=self._config.tfidf_min_df,
            ngram_range=self._config.tfidf_ngram_range,
            token_pattern=r"[a-z_]+(?:\d+)?",
            stop_words=list(_STOP_WORDS),
        )

        try:
            return vectorizer.fit_transform(messages)
        except ValueError:
            return None

    # --- Distance matrix ---

    def _build_condensed_distance_matrix(
        self,
        features: list[FailureFeatures],
        message_sim_matrix: np.ndarray | None,
    ) -> np.ndarray:
        """Построить condensed distance matrix для scipy linkage.

        Возвращает 1-D массив из n*(n-1)/2 попарных расстояний.
        """
        n = len(features)
        condensed = np.zeros(n * (n - 1) // 2, dtype=np.float64)

        idx = 0
        for i in range(n):
            for j in range(i + 1, n):
                msg_sim = (
                    float(message_sim_matrix[i, j])
                    if message_sim_matrix is not None
                    else None
                )
                condensed[idx] = self._pairwise_distance(
                    features[i], features[j], msg_sim,
                )
                idx += 1

        return condensed

    def _pairwise_distance(
        self,
        a: FailureFeatures,
        b: FailureFeatures,
        message_cosine_sim: float | None,
    ) -> float:
        """Композитное расстояние между двумя ошибками.

        Отсутствующие сигналы исключаются, их вес перераспределяется.
        Если нет ни одного активного сигнала — возвращает 1.0.
        """
        cfg = self._config
        active: list[tuple[float, float]] = []  # (weight, distance)

        # Сигнал 1: тип исключения (exact match)
        if a.exception_type is not None and b.exception_type is not None:
            d = 0.0 if a.exception_type == b.exception_type else 1.0
            active.append((cfg.weight_exception_type, d))

        # Сигнал 2: сообщение (TF-IDF cosine distance)
        if a.normalized_message and b.normalized_message:
            if message_cosine_sim is not None:
                d = 1.0 - max(0.0, min(1.0, message_cosine_sim))
            else:
                d = 1.0
            active.append((cfg.weight_message, d))

        # Сигнал 3: фреймы трейса (set Jaccard distance)
        if a.trace_frame_set and b.trace_frame_set:
            intersection = len(a.trace_frame_set & b.trace_frame_set)
            union = len(a.trace_frame_set | b.trace_frame_set)
            jaccard_sim = intersection / union if union > 0 else 0.0
            d = 1.0 - jaccard_sim
            active.append((cfg.weight_trace, d))

        # Сигнал 4: категория (exact match)
        if a.category is not None and b.category is not None:
            d = 0.0 if a.category == b.category else 1.0
            active.append((cfg.weight_category, d))

        if not active:
            return 1.0

        total_weight = sum(w for w, _ in active)
        return sum(w * d for w, d in active) / total_weight

    # --- Cluster building ---

    def _build_cluster(
        self,
        group_features: list[FailureFeatures],
        failure_map: dict[int, FailedTestSummary],
    ) -> FailureCluster:
        """Создать FailureCluster из группы features."""
        # Представитель: больше всего сигналов, при равенстве — меньший ID
        representative = max(
            group_features,
            key=lambda f: (f.signal_count, -f.test_result_id),
        )

        member_ids = sorted(f.test_result_id for f in group_features)
        rep_failure = failure_map[representative.test_result_id]

        signature = ClusterSignature(
            exception_type=representative.exception_type,
            message_pattern=representative.raw_message_prefix,
            common_frames=list(representative.trace_frames_ordered),
            category=representative.category,
        )

        label = self._generate_label(signature, group_features)

        return FailureCluster(
            cluster_id=self._generate_cluster_id(signature),
            label=label,
            signature=signature,
            member_test_ids=member_ids,
            member_count=len(member_ids),
            example_message=rep_failure.status_message,
            example_trace_snippet=_first_n_lines(
                rep_failure.status_trace,
                self._config.trace_snippet_lines,
            ),
        )

    def _generate_label(
        self,
        signature: ClusterSignature,
        group_features: list[FailureFeatures],
    ) -> str:
        """Сгенерировать человекочитаемую метку кластера."""
        parts: list[str] = []

        if signature.exception_type:
            parts.append(signature.exception_type)

        if signature.common_frames:
            location = signature.common_frames[0]
            segments = location.rsplit(".", 2)
            short = ".".join(segments[-2:]) if len(segments) >= 2 else location
            parts.append(f"in {short}")

        if not parts and signature.message_pattern:
            msg = signature.message_pattern.strip()
            if len(msg) > 80:
                msg = msg[:77] + "..."
            parts.append(msg)

        if not parts and signature.category:
            parts.append(f"Category: {signature.category}")

        if not parts:
            if group_features:
                best = max(group_features, key=lambda f: f.signal_count)
                parts.append(f"Test: {best.test_result_id}")
            else:
                parts.append("Unclassified failure")

        label = " ".join(parts)
        return label[: self._config.max_label_length]

    def _generate_cluster_id(self, signature: ClusterSignature) -> str:
        """Детерминированный ID кластера на основе SHA-256 хеша сигнатуры."""
        components = [
            signature.exception_type or "",
            signature.category or "",
            "|".join(signature.common_frames),
            signature.message_pattern or "",
        ]
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
