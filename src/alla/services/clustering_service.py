"""Сервис кластеризации похожих ошибок тестов по корневой причине.

Алгоритм: text-first подход.
1. Из каждого упавшего теста собирается один текстовый документ
   (message + trace + category) без разбора на составные части.
2. Минимальная нормализация: замена волатильных данных (UUID, timestamps,
   длинные числа, IP) плейсхолдерами.
3. TF-IDF + cosine distance — TF-IDF сам определяет, какие слова/фразы
   важны для различения ошибок. Работает с любым языком (латиница,
   кириллица, смешанный) и любым форматом ошибок.
4. Agglomerative clustering (complete linkage) — гарантирует, что каждая
   пара тестов в кластере имеет расстояние < порога.
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
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def _normalize_text(text: str) -> str:
    """Заменить волатильные данные плейсхолдерами.

    Не трогаем саму структуру текста, не удаляем стоп-слова,
    не приводим к lowercase — всё это делает TfidfVectorizer.
    """
    text = _UUID_RE.sub("<ID>", text)
    text = _TIMESTAMP_RE.sub("<TS>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _LONG_NUMBER_RE.sub("<NUM>", text)
    return text


def _build_document(failure: FailedTestSummary) -> str:
    """Собрать один текстовый документ из всей доступной информации об ошибке.

    Конкатенация: message + trace + category.
    Без парсинга, без извлечения типов исключений, без разбора фреймов.
    """
    parts: list[str] = []

    if failure.status_message:
        parts.append(failure.status_message)

    if failure.status_trace:
        parts.append(failure.status_trace)

    if failure.category:
        parts.append(failure.category)

    raw = "\n".join(parts)
    return _normalize_text(raw) if raw else ""


# ---------------------------------------------------------------------------
# ClusteringService
# ---------------------------------------------------------------------------

class ClusteringService:
    """Группирует похожие ошибки тестов в кластеры по корневой причине.

    Алгоритм: TF-IDF по полному тексту ошибки + agglomerative clustering
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

        # 1. Собрать текстовые документы
        documents: list[str] = [_build_document(f) for f in failures]

        # 2. Разделить на тесты с текстом и без
        has_text_indices: list[int] = []
        empty_indices: list[int] = []
        for i, doc in enumerate(documents):
            if doc.strip():
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
            text_docs = [documents[i] for i in has_text_indices]
            labels = self._cluster_texts(text_docs)

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

    def _cluster_texts(self, documents: list[str]) -> list[int]:
        """TF-IDF + agglomerative clustering по текстам.

        Возвращает список меток кластеров (одна метка на документ).
        """
        # TF-IDF с Unicode token_pattern — работает с любым языком
        vectorizer = TfidfVectorizer(
            max_features=self._config.tfidf_max_features,
            ngram_range=self._config.tfidf_ngram_range,
            token_pattern=r"(?u)\b\w\w+\b",
            lowercase=True,
        )

        try:
            tfidf_matrix = vectorizer.fit_transform(documents)
        except ValueError:
            # Все документы пустые после токенизации
            return list(range(len(documents)))

        # Cosine distance matrix
        sim_matrix = cosine_similarity(tfidf_matrix)
        np.clip(sim_matrix, 0.0, 1.0, out=sim_matrix)
        dist_matrix = 1.0 - sim_matrix

        # Condensed form для scipy
        n = len(documents)
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
            cluster_id=self._generate_cluster_id(signature),
            label=label,
            signature=signature,
            member_test_ids=member_ids,
            member_count=len(member_ids),
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
    def _generate_cluster_id(signature: ClusterSignature) -> str:
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
