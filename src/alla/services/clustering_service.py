"""Сервис кластеризации похожих ошибок тестов по корневой причине.

Алгоритм: multi-signal fingerprinting + single-pass greedy clustering.
Без внешних ML-зависимостей — чистый Python, regex, set operations.
Детерминированный, воспроизводимый результат.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

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

    weight_exception_type: float = 0.30
    weight_message: float = 0.35
    weight_trace: float = 0.25
    weight_category: float = 0.10

    trace_root_frames: int = 5
    max_label_length: int = 120
    trace_snippet_lines: int = 5

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


# ---------------------------------------------------------------------------
# ErrorFingerprint — extracted features for comparison
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorFingerprint:
    """Извлечённые фичи одной ошибки для сравнения."""

    exception_type: str | None
    message_tokens: frozenset[str]
    root_frames: tuple[str, ...]
    category: str | None
    raw_message_prefix: str | None


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

    def extract(self, failure: FailedTestSummary) -> ErrorFingerprint:
        """Извлечь ``ErrorFingerprint`` из ``FailedTestSummary``."""
        exception_type = self.extract_exception_type(
            failure.status_message, failure.status_trace,
        )
        message_tokens = self.normalize_message(failure.status_message)
        root_frames = self.normalize_trace(failure.status_trace)
        raw_prefix = failure.status_message[:100] if failure.status_message else None

        return ErrorFingerprint(
            exception_type=exception_type,
            message_tokens=message_tokens,
            root_frames=root_frames,
            category=failure.category,
            raw_message_prefix=raw_prefix,
        )

    def extract_exception_type(
        self,
        message: str | None,
        trace: str | None,
    ) -> str | None:
        """Определить тип исключения из сообщения или трейса."""
        # Приоритет: первая строка trace (обычно содержит класс исключения),
        # затем message.
        sources = []
        if trace:
            sources.append(trace.strip().splitlines()[0] if trace.strip() else "")
        if message:
            sources.append(message)

        for text in sources:
            # Java-style: com.example.SomeException: msg
            m = _JAVA_EXCEPTION_RE.search(text)
            if m:
                return m.group(1)

            # Python-style: SomeError: msg
            m = _PYTHON_EXCEPTION_RE.search(text)
            if m:
                return m.group(1)

        # Специальные паттерны, если стандартные не сработали
        combined = " ".join(s for s in sources if s)
        if _TIMEOUT_RE.search(combined):
            return "TimeoutError"

        # Приоритет: статус ошибки (после "got"/"received"), затем любой статус
        m = _HTTP_STATUS_ERROR_RE.search(combined)
        if m:
            return f"HTTPStatus{m.group(1)}"
        m = _HTTP_STATUS_RE.search(combined)
        if m:
            return f"HTTPStatus{m.group(1)}"

        return None

    def normalize_message(self, message: str | None) -> frozenset[str]:
        """Токенизировать и нормализовать сообщение об ошибке для сравнения."""
        if not message:
            return frozenset()

        text = message.lower()
        text = _UUID_RE.sub("<uuid>", text)
        text = _TIMESTAMP_RE.sub("<ts>", text)
        text = _IP_RE.sub("<ip>", text)
        text = _PATH_RE.sub("<path>", text)
        text = _NUMBER_RE.sub("<num>", text)

        tokens = re.findall(r"[a-z_<>]+(?:\d+)?", text)
        return frozenset(
            t for t in tokens if t not in _STOP_WORDS and len(t) > 1
        )

    def normalize_trace(self, trace: str | None) -> tuple[str, ...]:
        """Извлечь и нормализовать top-N фреймов стек-трейса (причина ошибки)."""
        if not trace:
            return ()

        frames: list[str] = []
        for line in trace.strip().splitlines():
            line = line.strip()

            # Java: at com.example.Class.method(File.java:42)
            m = _JAVA_FRAME_RE.search(line)
            if m:
                frames.append(m.group(1))
                continue

            # Python: File "/path/to/module.py", line 42, in func_name
            m = _PYTHON_FRAME_RE.search(line)
            if m:
                frames.append(f"{m.group(1)}:{m.group(2)}")
                continue

        # Фильтрация фреймворк-пакетов
        frames = [
            f for f in frames
            if not any(f.startswith(pkg) for pkg in self._config.ignored_frame_packages)
        ]

        # Top-N фреймов — ближайшие к причине ошибки
        # В Java trace причина вверху, в Python — тоже (последний вызов = верх)
        root_count = self._config.trace_root_frames
        return tuple(frames[:root_count])


# ---------------------------------------------------------------------------
# ClusteringService
# ---------------------------------------------------------------------------

class ClusteringService:
    """Группирует похожие ошибки тестов в кластеры по корневой причине.

    Алгоритм: single-pass greedy assignment с взвешенной
    multi-signal метрикой схожести. Детерминированный, без зависимостей.
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

        # 1. Извлечение fingerprint для каждого теста
        items: list[tuple[FailedTestSummary, ErrorFingerprint]] = [
            (f, self._extractor.extract(f)) for f in failures
        ]

        # 2. Сортировка по test_result_id для детерминизма
        items.sort(key=lambda pair: pair[0].test_result_id)

        # 3. Greedy single-pass clustering
        # Каждый кластер: (representative_fp, representative_failure, member_ids)
        clusters: list[tuple[ErrorFingerprint, FailedTestSummary, list[int]]] = []

        for failure, fp in items:
            best_idx = -1
            best_score = 0.0

            for idx, (rep_fp, _, _) in enumerate(clusters):
                score = self._compute_similarity(fp, rep_fp)
                if score > best_score:
                    best_score = score
                    best_idx = idx

            if best_score >= self._config.similarity_threshold and best_idx >= 0:
                clusters[best_idx][2].append(failure.test_result_id)
            else:
                clusters.append((fp, failure, [failure.test_result_id]))

        # 4. Конвертация в выходные модели
        result_clusters: list[FailureCluster] = []
        for rep_fp, rep_failure, member_ids in clusters:
            signature = ClusterSignature(
                exception_type=rep_fp.exception_type,
                message_pattern=rep_fp.raw_message_prefix,
                common_frames=list(rep_fp.root_frames),
                category=rep_fp.category,
            )
            result_clusters.append(FailureCluster(
                cluster_id=self._generate_cluster_id(signature),
                label=self._generate_label(signature),
                signature=signature,
                member_test_ids=member_ids,
                member_count=len(member_ids),
                example_message=rep_failure.status_message,
                example_trace_snippet=_first_n_lines(
                    rep_failure.status_trace,
                    self._config.trace_snippet_lines,
                ),
            ))

        # Сортировка: самые крупные кластеры первыми
        result_clusters.sort(key=lambda c: c.member_count, reverse=True)

        logger.info(
            "Clustered %d failures into %d clusters",
            len(failures),
            len(result_clusters),
        )

        return ClusteringReport(
            launch_id=launch_id,
            total_failures=len(failures),
            cluster_count=len(result_clusters),
            clusters=result_clusters,
        )

    def _compute_similarity(
        self,
        a: ErrorFingerprint,
        b: ErrorFingerprint,
    ) -> float:
        """Взвешенная multi-signal метрика схожести двух fingerprints."""
        cfg = self._config
        score = 0.0

        # Сигнал 1: совпадение типа исключения (binary)
        if a.exception_type and b.exception_type:
            if a.exception_type == b.exception_type:
                score += cfg.weight_exception_type
        elif a.exception_type is None and b.exception_type is None:
            score += cfg.weight_exception_type * 0.5

        # Сигнал 2: Jaccard similarity по токенам сообщения
        if a.message_tokens and b.message_tokens:
            intersection = len(a.message_tokens & b.message_tokens)
            union = len(a.message_tokens | b.message_tokens)
            jaccard = intersection / union if union > 0 else 0.0
            score += cfg.weight_message * jaccard
        elif not a.message_tokens and not b.message_tokens:
            score += cfg.weight_message * 0.5

        # Сигнал 3: совпадение корневых фреймов стек-трейса
        if a.root_frames and b.root_frames:
            common = 0
            for fa, fb in zip(a.root_frames, b.root_frames):
                if fa == fb:
                    common += 1
                else:
                    break
            max_len = max(len(a.root_frames), len(b.root_frames))
            frame_ratio = common / max_len if max_len > 0 else 0.0
            score += cfg.weight_trace * frame_ratio
        elif not a.root_frames and not b.root_frames:
            score += cfg.weight_trace * 0.5

        # Сигнал 4: совпадение категории (binary)
        if a.category and b.category:
            if a.category == b.category:
                score += cfg.weight_category
        elif a.category is None and b.category is None:
            score += cfg.weight_category * 0.5

        return score

    def _generate_label(self, signature: ClusterSignature) -> str:
        """Сгенерировать человекочитаемую метку кластера."""
        parts: list[str] = []

        if signature.exception_type:
            parts.append(signature.exception_type)

        if signature.common_frames:
            location = signature.common_frames[0]
            # Сократить до последних 2 сегментов: com.example.UserService.getUser → UserService.getUser
            segments = location.rsplit(".", 2)
            short = ".".join(segments[-2:]) if len(segments) >= 2 else location
            parts.append(f"in {short}")

        if not parts and signature.message_pattern:
            parts.append(signature.message_pattern[:80])

        if not parts and signature.category:
            parts.append(f"Category: {signature.category}")

        if not parts:
            parts.append("Unknown error")

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
