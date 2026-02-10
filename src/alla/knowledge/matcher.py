"""Алгоритм сопоставления ошибок с записями базы знаний."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from alla.knowledge.models import KBEntry, KBMatchResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatcherConfig:
    """Параметры алгоритма сопоставления."""

    # Веса для keyword stage
    exception_type_weight: float = 0.35
    message_pattern_weight: float = 0.25
    trace_pattern_weight: float = 0.15
    category_weight: float = 0.10
    keyword_weight: float = 0.15

    # Веса для объединения keyword score и TF-IDF score
    keyword_stage_weight: float = 0.6
    tfidf_stage_weight: float = 0.4

    # TF-IDF параметры
    tfidf_max_features: int = 500
    tfidf_ngram_range: tuple[int, int] = field(default=(1, 2))

    # Фильтрация результатов
    min_score: float = 0.1
    max_results: int = 5


class TextMatcher:
    """Сопоставляет текст ошибки с записями KB.

    Двухэтапный алгоритм:
    1. Детерминистический keyword/pattern matching (быстрый, точный).
    2. TF-IDF cosine similarity (нечёткий, ловит вариации).
    Итоговый score — взвешенная комбинация обоих этапов.
    """

    def __init__(self, config: MatcherConfig | None = None) -> None:
        self._config = config or MatcherConfig()

    def match(
        self,
        query_message: str | None,
        query_trace: str | None,
        query_category: str | None,
        entries: list[KBEntry],
        *,
        min_score: float | None = None,
        max_results: int | None = None,
    ) -> list[KBMatchResult]:
        """Сопоставить ошибку со списком записей KB.

        Args:
            query_message: Сообщение об ошибке.
            query_trace: Стек-трейс.
            query_category: Категория ошибки.
            entries: Записи KB для сопоставления.
            min_score: Переопределить MatcherConfig.min_score для этого вызова.
            max_results: Переопределить MatcherConfig.max_results для этого вызова.

        Returns:
            Список KBMatchResult, отсортированный по score (desc),
            отфильтрованный по min_score и ограниченный max_results.
        """
        effective_min_score = min_score if min_score is not None else self._config.min_score
        effective_max_results = max_results if max_results is not None else self._config.max_results

        if not entries:
            return []

        combined_query = _combine_text(query_message, query_trace, query_category)
        if not combined_query.strip():
            return []

        # Этап 1: keyword/pattern matching
        keyword_scores: list[tuple[float, list[str]]] = []
        for entry in entries:
            score, matched_on = self._keyword_match(
                query_message, query_trace, query_category, entry,
            )
            keyword_scores.append((score, matched_on))

        # Этап 2: TF-IDF cosine similarity
        tfidf_scores = self._compute_tfidf_scores(combined_query, entries)

        # Объединение
        kw_w = self._config.keyword_stage_weight
        tf_w = self._config.tfidf_stage_weight
        results: list[KBMatchResult] = []
        for i, entry in enumerate(entries):
            kw_score, matched_on = keyword_scores[i]
            tf_score = tfidf_scores[i] if tfidf_scores else 0.0
            blended = kw_w * kw_score + tf_w * tf_score
            if logger.isEnabledFor(logging.DEBUG) and (kw_score > 0 or tf_score > 0.05):
                logger.debug(
                    "KB scoring: id=%s, keyword=%.4f, tfidf=%.4f, "
                    "blended=%.4f (порог=%.2f) %s",
                    entry.id,
                    kw_score,
                    tf_score,
                    blended,
                    effective_min_score,
                    "✓ ПРОШЛО" if blended >= effective_min_score else "✗ отсеяно",
                )
            if blended >= effective_min_score:
                results.append(KBMatchResult(
                    entry=entry,
                    score=round(blended, 4),
                    matched_on=matched_on,
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        final = results[:effective_max_results]

        if logger.isEnabledFor(logging.DEBUG):
            query_preview = (combined_query[:150].replace("\n", " ")
                             if combined_query else "<пусто>")
            if final:
                for r in final:
                    logger.debug(
                        "KB совпадение: запись='%s' (id=%s), score=%.4f, "
                        "совпало по: [%s], запрос='%s'",
                        r.entry.title,
                        r.entry.id,
                        r.score,
                        "; ".join(r.matched_on) if r.matched_on else "tfidf only",
                        query_preview,
                    )
            else:
                logger.debug(
                    "KB: нет совпадений (min_score=%.2f) для запроса='%s'",
                    effective_min_score,
                    query_preview,
                )

        return final

    def _keyword_match(
        self,
        message: str | None,
        trace: str | None,
        category: str | None,
        entry: KBEntry,
    ) -> tuple[float, list[str]]:
        """Детерминистический keyword/pattern matching."""
        score = 0.0
        matched_on: list[str] = []
        msg_lower = (message or "").lower()
        trace_lower = (trace or "").lower()
        cat_lower = (category or "").lower()
        combined_lower = f"{msg_lower} {trace_lower} {cat_lower}"
        criteria = entry.match_criteria

        # Exception types
        if criteria.exception_types:
            matched = [
                et for et in criteria.exception_types
                if et.lower() in msg_lower or et.lower() in trace_lower
            ]
            if matched:
                ratio = len(matched) / len(criteria.exception_types)
                score += self._config.exception_type_weight * ratio
                matched_on.append(f"exception_type: {', '.join(matched)}")

        # Message patterns
        if criteria.message_patterns:
            matched = [
                mp for mp in criteria.message_patterns
                if mp.lower() in msg_lower
            ]
            if matched:
                ratio = len(matched) / len(criteria.message_patterns)
                score += self._config.message_pattern_weight * ratio
                matched_on.append(f"message_pattern: {', '.join(matched[:3])}")

        # Trace patterns
        if criteria.trace_patterns:
            matched = [
                tp for tp in criteria.trace_patterns
                if tp.lower() in trace_lower
            ]
            if matched:
                ratio = len(matched) / len(criteria.trace_patterns)
                score += self._config.trace_pattern_weight * ratio
                matched_on.append(f"trace_pattern: {', '.join(matched[:3])}")

        # Category
        if criteria.categories and cat_lower:
            matched = [
                c for c in criteria.categories
                if c.lower() in cat_lower or cat_lower in c.lower()
            ]
            if matched:
                score += self._config.category_weight
                matched_on.append(f"category: {', '.join(matched)}")

        # Keywords
        if criteria.keywords:
            matched = [
                kw for kw in criteria.keywords
                if kw.lower() in combined_lower
            ]
            if matched:
                ratio = len(matched) / len(criteria.keywords)
                score += self._config.keyword_weight * ratio
                matched_on.append(f"keyword: {', '.join(matched[:5])}")

        return score, matched_on

    def _compute_tfidf_scores(
        self,
        query_text: str,
        entries: list[KBEntry],
    ) -> list[float]:
        """Вычислить TF-IDF cosine similarity между запросом и каждой KB-записью."""
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        kb_documents = [_entry_to_document(e) for e in entries]
        all_docs = [query_text, *kb_documents]

        try:
            vectorizer = TfidfVectorizer(
                max_features=self._config.tfidf_max_features,
                ngram_range=self._config.tfidf_ngram_range,
                token_pattern=r"(?u)\b\w\w+\b",
                lowercase=True,
            )
            tfidf = vectorizer.fit_transform(all_docs)
        except ValueError:
            # Пустой словарь после фильтрации
            return []

        query_vec = tfidf[0:1]
        kb_vecs = tfidf[1:]
        sims = cosine_similarity(query_vec, kb_vecs)[0]
        return [float(max(0.0, min(1.0, s))) for s in sims]


def _combine_text(
    message: str | None, trace: str | None, category: str | None,
) -> str:
    """Собрать query-документ из доступных текстов."""
    parts = [p for p in (message, trace, category) if p]
    return "\n".join(parts)


def _entry_to_document(entry: KBEntry) -> str:
    """Конвертировать match_criteria KB-записи в текстовый документ для TF-IDF."""
    parts: list[str] = []
    mc = entry.match_criteria
    parts.extend(mc.keywords)
    parts.extend(mc.message_patterns)
    parts.extend(mc.trace_patterns)
    parts.extend(mc.exception_types)
    parts.extend(mc.categories)
    parts.append(entry.title)
    parts.append(entry.description)
    return " ".join(parts)
