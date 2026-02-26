"""Трёхуровневый алгоритм сопоставления ошибок с записями базы знаний.

Уровни (tiers):
  1. Exact substring — нормализованный error_example целиком найден в логе.
  2. Line match — ≥80% строк error_example найдены в логе (с небольшими отличиями).
  3. TF-IDF cosine similarity — нечёткий поиск (fallback), score ограничен сверху.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from alla.knowledge.models import KBEntry, KBMatchResult
from alla.utils.text_normalization import normalize_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MULTI_WS_RE = re.compile(r"\s+")


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

    # --- Tier 3: TF-IDF (fallback) ---
    tier3_score_cap: float = 0.5
    tfidf_max_features: int = 500
    tfidf_ngram_range: tuple[int, int] = (1, 2)
    example_weight: float = 0.8
    title_desc_weight: float = 0.2

    # --- Feedback boost ---
    boost_factor: float = 1.25


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(
        self,
        error_text: str,
        entries: list[KBEntry],
        *,
        query_label: str | None = None,
        exclusions: set[int] | None = None,
        boosts: set[int] | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки.

        Args:
            error_text: Объединённый текст ошибки (message + trace + logs).
            entries: Записи KB для сопоставления.
            query_label: Метка для отладочных логов (cluster_id).
            exclusions: entry_id записей с dislike — полностью исключаются.
            boosts: entry_id записей с like — score умножается на boost_factor.

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
            # Feedback exclusion: disliked entry для этого fingerprint
            if (
                exclusions
                and entry.entry_id is not None
                and entry.entry_id in exclusions
            ):
                logger.debug(
                    "KB excluded%s: '%s' (entry_id=%d) — disliked",
                    f" [{query_label}]" if query_label else "",
                    entry.title, entry.entry_id,
                )
                continue

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

        # --- Feedback boosts: увеличить score liked записей ---
        if boosts:
            for r in results:
                if r.entry.entry_id is not None and r.entry.entry_id in boosts:
                    original = r.score
                    r.score = round(min(1.0, r.score * cfg.boost_factor), 4)
                    r.matched_on.append(
                        f"Boosted: {original:.2f} → {r.score:.2f} (liked)"
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

    def _tier2_line_match(
        self,
        query_collapsed: str,
        example_normalized: str,
    ) -> float | None:
        """Построчное сопоставление: доля строк error_example, найденных в query.

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
            if line in query_collapsed
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

        Returns:
            Список (local_index, capped_score, example_sim, title_desc_sim)
            для записей выше min_score.
        """
        cfg = self._config
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
