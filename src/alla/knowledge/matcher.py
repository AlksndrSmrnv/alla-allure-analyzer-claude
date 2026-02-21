"""Трёхуровневый алгоритм сопоставления ошибок с записями базы знаний.

Уровни (tiers):
  1. Exact substring — нормализованный error_example целиком найден в логе.
  2. Line match — ≥80% строк error_example найдены в логе (точно или по word-overlap).
  3. TF-IDF cosine similarity — нечёткий поиск (fallback), score ограничен сверху.
     Если вызван fit(), TF-IDF предобучается на корпусе KB один раз (стабильный IDF).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from alla.knowledge.models import KBEntry, KBMatchResult
from alla.utils.text_normalization import normalize_text

if TYPE_CHECKING:
    from scipy.sparse import spmatrix

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MULTI_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"(?u)\b\w\w+\b")


def _collapse_whitespace(text: str) -> str:
    """Свернуть все пробельные последовательности в один пробел."""
    return _MULTI_WS_RE.sub(" ", text).strip()


def _preview_head(text: str, max_chars: int) -> str:
    """Сжать head-preview для DEBUG-логов одной строкой."""
    return text[:max_chars].replace("\n", " ")


def _preview_tail(text: str, max_chars: int) -> str:
    """Сжать tail-preview для DEBUG-логов одной строкой."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatcherConfig:
    """Параметры алгоритма KB-matching."""

    # --- Общие ---
    min_score: float = 0.15
    max_results: int = 2

    # --- Tier 1: exact substring ---
    tier1_score: float = 1.0

    # --- Tier 2: line match ---
    tier2_line_threshold: float = 0.8
    tier2_score_min: float = 0.7
    tier2_score_max: float = 0.95
    tier2_min_lines: int = 2
    tier2_fuzzy_word_threshold: float = 0.75  # доля слов строки, найденных в query

    # --- Tier 3: TF-IDF (fallback) ---
    tier3_score_cap: float = 0.5
    tfidf_max_features: int = 500
    tfidf_ngram_range: tuple[int, int] = (1, 2)
    example_weight: float = 0.8
    title_desc_weight: float = 0.2


# ---------------------------------------------------------------------------
# TextMatcher
# ---------------------------------------------------------------------------


class TextMatcher:
    """Сопоставляет текст ошибки с записями KB через 3-уровневый алгоритм.

    Tier 1 — exact substring: нормализованный error_example (whitespace collapsed)
        целиком содержится в нормализованном query. Score = 1.0.
    Tier 2 — line match: ≥80% строк error_example найдены в query. Score ∈ [0.7, 0.95].
    Tier 3 — TF-IDF cosine similarity (текущий нечёткий алгоритм). Score ≤ 0.5.
    """

    def __init__(self, config: MatcherConfig | None = None) -> None:
        self._config = config or MatcherConfig()
        # Pre-trained TF-IDF (заполняется через fit())
        self._fitted_vectorizer: TfidfVectorizer | None = None
        self._fitted_example_matrix: spmatrix | None = None
        self._fitted_title_desc_matrix: spmatrix | None = None
        self._entry_index: dict[str, int] = {}  # entry.id → строка в матрице

    # ------------------------------------------------------------------
    # Pre-training
    # ------------------------------------------------------------------

    def fit(self, entries: list[KBEntry]) -> None:
        """Предобучить TF-IDF на всём корпусе KB.

        После вызова Tier 3 использует стабильный IDF (не зависящий от query)
        и быстрый transform() вместо fit_transform() на каждый поиск.
        Если entries пустые — сбрасывает предобученное состояние.

        Args:
            entries: Список записей KB для предобучения.
        """
        if not entries:
            self._fitted_vectorizer = None
            self._fitted_example_matrix = None
            self._fitted_title_desc_matrix = None
            self._entry_index = {}
            return

        cfg = self._config
        example_docs = [normalize_text(e.error_example) for e in entries]
        title_desc_docs = [
            f"{e.title} {e.description}".strip() for e in entries
        ]

        vectorizer = TfidfVectorizer(
            max_features=cfg.tfidf_max_features,
            ngram_range=cfg.tfidf_ngram_range,
            token_pattern=r"(?u)\b\w\w+\b",
            lowercase=True,
        )
        try:
            all_matrix = vectorizer.fit_transform(example_docs + title_desc_docs)
        except ValueError:
            logger.debug("KB TextMatcher.fit(): TF-IDF fit failed (пустые документы)")
            return

        n = len(entries)
        self._fitted_vectorizer = vectorizer
        self._fitted_example_matrix = all_matrix[:n]
        self._fitted_title_desc_matrix = all_matrix[n:]
        self._entry_index = {e.id: i for i, e in enumerate(entries)}
        logger.debug(
            "KB TextMatcher.fit(): предобучен на %d записях KB", n,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(
        self,
        error_text: str,
        entries: list[KBEntry],
        *,
        query_label: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки.

        Args:
            error_text: Объединённый текст ошибки (message + trace + logs).
            entries: Записи KB для сопоставления.
            query_label: Метка для отладочных логов (cluster_id).

        Returns:
            Список KBMatchResult, отсортированный по score desc.
        """
        if not error_text or not error_text.strip():
            return []
        if not entries:
            return []

        cfg = self._config

        # --- Нормализация query один раз ---
        query_normalized = normalize_text(error_text)
        query_collapsed = _collapse_whitespace(query_normalized)

        # --- Подготовка KB-документов один раз ---
        example_normalized: list[str] = []
        example_collapsed: list[str] = []
        title_desc_docs: list[str] = []
        for entry in entries:
            norm = normalize_text(entry.error_example)
            example_normalized.append(norm)
            example_collapsed.append(_collapse_whitespace(norm))
            title_desc_docs.append(f"{entry.title} {entry.description}".strip())

        results: list[KBMatchResult] = []
        tier3_needed_indices: list[int] = []

        # --- Tier 1 + Tier 2: per-entry ---
        for i, entry in enumerate(entries):
            # Tier 1: exact substring
            t1_score = self._tier1_exact_substring(
                query_collapsed, example_collapsed[i],
            )
            if t1_score is not None and t1_score >= cfg.min_score:
                results.append(KBMatchResult(
                    entry=entry,
                    score=round(t1_score, 4),
                    matched_on=[f"Tier 1: exact substring match (score={t1_score:.2f})"],
                ))
                logger.debug(
                    "KB Tier 1%s: '%s' (id=%s), score=%.4f",
                    f" [{query_label}]" if query_label else "",
                    entry.title, entry.id, t1_score,
                )
                continue

            # Tier 2: line match
            t2_score = self._tier2_line_match(
                query_collapsed, example_normalized[i],
            )
            if t2_score is not None and t2_score >= cfg.min_score:
                results.append(KBMatchResult(
                    entry=entry,
                    score=round(t2_score, 4),
                    matched_on=[f"Tier 2: line match (score={t2_score:.2f})"],
                ))
                logger.debug(
                    "KB Tier 2%s: '%s' (id=%s), score=%.4f",
                    f" [{query_label}]" if query_label else "",
                    entry.title, entry.id, t2_score,
                )
                continue

            # Не совпало по Tier 1/2 → кандидат для Tier 3
            tier3_needed_indices.append(i)

        # --- Tier 3: TF-IDF только для непойманных записей ---
        if tier3_needed_indices:
            tier3_entries = [entries[i] for i in tier3_needed_indices]
            tier3_example_docs = [example_normalized[i] for i in tier3_needed_indices]
            tier3_title_desc = [title_desc_docs[i] for i in tier3_needed_indices]

            tier3_hits = self._tier3_tfidf(
                query_normalized,
                tier3_entries,
                tier3_example_docs,
                tier3_title_desc,
                query_label=query_label,
            )
            for local_idx, capped_score, ex_sim, td_sim in tier3_hits:
                entry = tier3_entries[local_idx]
                results.append(KBMatchResult(
                    entry=entry,
                    score=round(capped_score, 4),
                    matched_on=[
                        f"Tier 3: TF-IDF similarity: {ex_sim:.2f} (example), "
                        f"{td_sim:.2f} (title+desc), "
                        f"capped={capped_score:.2f}",
                    ],
                ))
                logger.debug(
                    "KB Tier 3%s: '%s' (id=%s), score=%.4f "
                    "(example=%.4f, title_desc=%.4f)",
                    f" [{query_label}]" if query_label else "",
                    entry.title, entry.id, capped_score, ex_sim, td_sim,
                )

        # --- Сортировка + лимит ---
        results.sort(key=lambda r: -r.score)
        results = results[: cfg.max_results]

        if not results and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "KB: нет совпадений%s (query_len=%d, head='%s', tail='%s')",
                f" [{query_label}]" if query_label else "",
                len(error_text),
                _preview_head(error_text, 220),
                _preview_tail(error_text, 220),
            )

        return results

    # ------------------------------------------------------------------
    # Tier 1: exact substring
    # ------------------------------------------------------------------

    def _tier1_exact_substring(
        self,
        query_collapsed: str,
        example_collapsed: str,
    ) -> float | None:
        """Проверить, содержится ли error_example целиком в query.

        Оба аргумента должны быть уже normalize_text() + _collapse_whitespace().

        Returns:
            tier1_score если подстрока найдена, иначе None.
        """
        if not example_collapsed:
            return None
        if example_collapsed in query_collapsed:
            return self._config.tier1_score
        return None

    # ------------------------------------------------------------------
    # Tier 2: line match
    # ------------------------------------------------------------------

    @staticmethod
    def _line_soft_matches(
        line: str,
        query_collapsed: str,
        word_threshold: float,
    ) -> bool:
        """Проверить совпадение строки с query: точно или по word-overlap.

        Сначала пробует точное вхождение (как раньше). Если не нашло —
        считает долю слов строки, присутствующих в query (case-insensitive).
        Строки короче 2 слов не получают fuzzy-совпадение.

        Args:
            line: Строка из KB error_example (whitespace collapsed, нормализована).
            query_collapsed: Весь query (whitespace collapsed, нормализован).
            word_threshold: Минимальная доля слов для fuzzy-совпадения.
        """
        if line in query_collapsed:
            return True
        words = _WORD_RE.findall(line)
        if len(words) < 2:
            return False
        query_lower = query_collapsed.lower()
        matched = sum(1 for w in words if w.lower() in query_lower)
        return matched / len(words) >= word_threshold

    def _tier2_line_match(
        self,
        query_collapsed: str,
        example_normalized: str,
    ) -> float | None:
        """Построчное сопоставление: доля строк error_example, найденных в query.

        Каждая строка проверяется точно или по word-overlap (fuzzy).

        Args:
            query_collapsed: normalize_text() + _collapse_whitespace() от query.
            example_normalized: normalize_text() от error_example (не collapsed).

        Returns:
            Score ∈ [tier2_score_min, tier2_score_max] если порог пройден,
            иначе None.
        """
        cfg = self._config

        # Разбиваем example на непустые строки, коллапсируем whitespace каждой
        example_lines = [
            _collapse_whitespace(line)
            for line in example_normalized.splitlines()
            if line.strip()
        ]

        if len(example_lines) < cfg.tier2_min_lines:
            return None

        matched_count = sum(
            1 for line in example_lines
            if self._line_soft_matches(
                line, query_collapsed, cfg.tier2_fuzzy_word_threshold,
            )
        )

        fraction = matched_count / len(example_lines)

        if fraction < cfg.tier2_line_threshold:
            return None

        # Линейная интерполяция: threshold → score_min, 1.0 → score_max
        denom = 1.0 - cfg.tier2_line_threshold
        if denom <= 0:
            return cfg.tier2_score_max if fraction >= 1.0 else None
        range_fraction = (fraction - cfg.tier2_line_threshold) / denom
        score = cfg.tier2_score_min + range_fraction * (
            cfg.tier2_score_max - cfg.tier2_score_min
        )
        return min(score, cfg.tier2_score_max)

    # ------------------------------------------------------------------
    # Tier 3: TF-IDF cosine similarity
    # ------------------------------------------------------------------

    def _tier3_tfidf(
        self,
        query_normalized: str,
        entries: list[KBEntry],
        example_docs: list[str],
        title_desc_docs: list[str],
        *,
        query_label: str | None = None,
    ) -> list[tuple[int, float, float, float]]:
        """Нечёткий TF-IDF поиск с ограничением score сверху.

        Если доступен предобученный векторайзер (после fit()), использует
        стабильный IDF корпуса KB и быстрый transform(). Иначе — fit_transform
        на лету (backwards-compatible fallback).

        Returns:
            Список (local_index, capped_score, example_sim, title_desc_sim)
            для записей выше min_score.
        """
        cfg = self._config

        if self._fitted_vectorizer is not None:
            return self._tier3_tfidf_pretrained(
                query_normalized, entries, query_label=query_label,
            )

        # Fallback: fit_transform на каждый запрос (старое поведение)
        n = len(entries)
        all_docs = [query_normalized] + example_docs + title_desc_docs

        vectorizer = TfidfVectorizer(
            max_features=cfg.tfidf_max_features,
            ngram_range=cfg.tfidf_ngram_range,
            token_pattern=r"(?u)\b\w\w+\b",
            lowercase=True,
        )
        try:
            tfidf_matrix = vectorizer.fit_transform(all_docs)
        except ValueError:
            logger.debug(
                "KB [%s]: TF-IDF fit_transform failed (пустые документы)",
                query_label or "?",
            )
            return []

        query_vec = tfidf_matrix[0:1]
        example_vecs = tfidf_matrix[1: n + 1]
        title_desc_vecs = tfidf_matrix[n + 1: 2 * n + 1]

        example_sims = cosine_similarity(query_vec, example_vecs)[0]
        title_desc_sims = cosine_similarity(query_vec, title_desc_vecs)[0]

        hits: list[tuple[int, float, float, float]] = []
        for i in range(n):
            ex_sim = float(max(0.0, example_sims[i]))
            td_sim = float(max(0.0, title_desc_sims[i]))
            raw_score = cfg.example_weight * ex_sim + cfg.title_desc_weight * td_sim
            capped_score = min(raw_score, cfg.tier3_score_cap)

            if capped_score < cfg.min_score:
                continue

            hits.append((i, capped_score, ex_sim, td_sim))

        return hits

    def _tier3_tfidf_pretrained(
        self,
        query_normalized: str,
        entries: list[KBEntry],
        *,
        query_label: str | None = None,
    ) -> list[tuple[int, float, float, float]]:
        """Tier 3 через предобученный TF-IDF (stабильный IDF корпуса KB).

        Использует только transform(query), без повторного fit.
        Обрабатывает только записи, чьи id есть в _entry_index
        (то есть те, что были в KB при вызове fit()).
        """
        cfg = self._config
        assert self._fitted_vectorizer is not None

        try:
            query_vec = self._fitted_vectorizer.transform([query_normalized])
        except Exception:
            logger.debug(
                "KB [%s]: pre-trained transform failed, fallback to fit_transform",
                query_label or "?",
            )
            return []

        all_example_sims = cosine_similarity(
            query_vec, self._fitted_example_matrix,
        )[0]
        all_td_sims = cosine_similarity(
            query_vec, self._fitted_title_desc_matrix,
        )[0]

        hits: list[tuple[int, float, float, float]] = []
        for local_i, entry in enumerate(entries):
            matrix_i = self._entry_index.get(entry.id)
            if matrix_i is None:
                continue
            ex_sim = float(max(0.0, all_example_sims[matrix_i]))
            td_sim = float(max(0.0, all_td_sims[matrix_i]))
            raw_score = cfg.example_weight * ex_sim + cfg.title_desc_weight * td_sim
            capped_score = min(raw_score, cfg.tier3_score_cap)

            if capped_score < cfg.min_score:
                continue

            hits.append((local_i, capped_score, ex_sim, td_sim))

        return hits
