"""Алгоритм сопоставления ошибок с записями базы знаний (TF-IDF cosine similarity)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from alla.knowledge.models import KBEntry, KBMatchResult
from alla.utils.text_normalization import normalize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatcherConfig:
    """Параметры алгоритма KB-matching."""

    min_score: float = 0.15
    max_results: int = 5
    tfidf_max_features: int = 500
    tfidf_ngram_range: tuple[int, int] = (1, 2)
    example_weight: float = 0.8
    title_desc_weight: float = 0.2


class TextMatcher:
    """Сопоставляет текст ошибки с записями KB через TF-IDF cosine similarity.

    Алгоритм:
    1. Нормализация текстов (UUID, timestamps, числа, IP → плейсхолдеры).
    2. TF-IDF vectorization всех документов (query + KB examples + KB title/desc).
    3. Cosine similarity между query и каждой KB-записью.
    4. Blended score = example_weight × example_sim + title_desc_weight × title_desc_sim.
    5. Фильтрация по min_score, сортировка, лимит max_results.
    """

    def __init__(self, config: MatcherConfig | None = None) -> None:
        self._config = config or MatcherConfig()

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

        # 1. Нормализация query
        query_normalized = normalize_text(error_text)

        # 2. Подготовка KB-документов
        example_docs: list[str] = []
        title_desc_docs: list[str] = []
        for entry in entries:
            example_docs.append(normalize_text(entry.error_example))
            title_desc_docs.append(
                f"{entry.title} {entry.description}".strip()
            )

        # 3. TF-IDF на объединённом корпусе
        #    Документы: [query, example_0, ..., example_N, title_desc_0, ..., title_desc_N]
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
            # Все документы пустые или не содержат токенов
            logger.debug(
                "KB [%s]: TF-IDF fit_transform failed (пустые документы)",
                query_label or "?",
            )
            return []

        # query — индекс 0
        query_vec = tfidf_matrix[0:1]

        # example docs — индексы [1, n+1)
        example_vecs = tfidf_matrix[1 : n + 1]

        # title_desc docs — индексы [n+1, 2n+1)
        title_desc_vecs = tfidf_matrix[n + 1 : 2 * n + 1]

        # 4. Cosine similarity
        example_sims = cosine_similarity(query_vec, example_vecs)[0]
        title_desc_sims = cosine_similarity(query_vec, title_desc_vecs)[0]

        # 5. Blended score + фильтрация
        results: list[KBMatchResult] = []
        for i, entry in enumerate(entries):
            ex_sim = float(max(0.0, example_sims[i]))
            td_sim = float(max(0.0, title_desc_sims[i]))

            score = (
                cfg.example_weight * ex_sim
                + cfg.title_desc_weight * td_sim
            )
            score = min(score, 1.0)

            if score < cfg.min_score:
                continue

            matched_on = [
                f"TF-IDF similarity: {ex_sim:.2f} (example), "
                f"{td_sim:.2f} (title+desc), "
                f"blended={score:.2f}",
            ]

            results.append(
                KBMatchResult(
                    entry=entry,
                    score=round(score, 4),
                    matched_on=matched_on,
                )
            )

            logger.debug(
                "KB совпадение%s: '%s' (id=%s), score=%.4f "
                "(example=%.4f, title_desc=%.4f)",
                f" [{query_label}]" if query_label else "",
                entry.title,
                entry.id,
                score,
                ex_sim,
                td_sim,
            )

        # Сортировка по score desc, лимит
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


def _preview_head(text: str, max_chars: int) -> str:
    """Сжать head-preview для DEBUG-логов одной строкой."""
    return text[:max_chars].replace("\n", " ")


def _preview_tail(text: str, max_chars: int) -> str:
    """Сжать tail-preview для DEBUG-логов одной строкой."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")
