"""PostgreSQL-реализация базы знаний.

Загружает все записи из PostgreSQL при инициализации (load-at-init-time),
кэширует их в памяти и делегирует поиск существующему TextMatcher.
Реализует контракт KnowledgeBaseProvider для PostgreSQL-бэкенда.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import psycopg  # psycopg[binary] — обязательная зависимость (см. pyproject.toml)

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.matcher import MatcherConfig, TextMatcher
from alla.knowledge.models import KBEntry, KBMatchResult, RootCauseCategory

if TYPE_CHECKING:
    from alla.knowledge.postgres_feedback import PostgresFeedbackStore

logger = logging.getLogger(__name__)


class PostgresKnowledgeBase:
    """Реализация KnowledgeBaseProvider, читающая записи из PostgreSQL.

    При инициализации выполняет один SELECT-запрос (глобальные записи +
    записи проекта), загружает результат в память и более не обращается к БД.

    Реализует Protocol KnowledgeBaseProvider.
    """

    def __init__(
        self,
        dsn: str,
        *,
        matcher_config: MatcherConfig | None = None,
        project_id: int | None = None,
        feedback_store: PostgresFeedbackStore | None = None,
    ) -> None:
        """
        Args:
            dsn: Строка подключения PostgreSQL в формате libpq / URI.
                 Пример: "postgresql://user:pass@host:5432/dbname"
            matcher_config: Конфигурация TextMatcher. None → defaults.
            project_id: ID проекта Allure TestOps. Если задан, загружаются
                        глобальные записи (project_id IS NULL) + записи этого
                        проекта (project_id = N). Если None — только глобальные.
            feedback_store: Хранилище обратной связи. Если задан,
                        search_by_error() загружает feedback-записи для fuzzy matching.
        """
        self._dsn = dsn
        self._project_id = project_id
        self._matcher = TextMatcher(config=matcher_config)
        self._feedback_store = feedback_store
        self._entries: list[KBEntry] = []
        self._entries_by_id: dict[str, KBEntry] = {}
        self._load()

    def _load(self) -> None:
        """Загрузить записи из PostgreSQL в память.

        Raises:
            KnowledgeBaseError: При ошибке подключения или ошибке запроса.
        """
        if self._project_id is not None:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       category, resolution_steps, project_id
                FROM alla.kb_entry
                WHERE project_id IS NULL OR project_id = %s
                ORDER BY project_id NULLS FIRST, id
            """
            params: tuple = (self._project_id,)
        else:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       category, resolution_steps, project_id
                FROM alla.kb_entry
                WHERE project_id IS NULL
                ORDER BY id
            """
            params = ()

        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка подключения к PostgreSQL KB: {exc}"
            ) from exc

        for row in rows:
            (
                pg_entry_id,
                slug,
                title,
                description,
                error_example,
                category_raw,
                resolution_steps,
                project_id,
            ) = row
            try:
                entry = KBEntry(
                    id=slug,
                    title=title,
                    description=description or "",
                    error_example=error_example,
                    category=RootCauseCategory(category_raw),
                    resolution_steps=list(resolution_steps or []),
                    entry_id=pg_entry_id,
                    project_id=project_id,
                )
            except Exception as exc:
                logger.warning(
                    "PostgresKB: ошибка валидации записи id=%r: %s. Пропущена.",
                    slug, exc,
                )
                continue

            if entry.id in self._entries_by_id:
                # ORDER BY project_id NULLS FIRST: глобальные загружаются первыми.
                # Проектная запись с тем же id переопределяет глобальную.
                logger.debug(
                    "PostgresKB: id=%r переопределена проектной записью (project_id=%s).",
                    entry.id, self._project_id,
                )
                self._entries = [e for e in self._entries if e.id != entry.id]
                self._entries_by_id.pop(entry.id, None)

            self._entries.append(entry)
            self._entries_by_id[entry.id] = entry
            logger.debug("PostgresKB: загружена запись id=%r (entry_id=%d)", entry.id, pg_entry_id)

        logger.info(
            "PostgresKB: загружено %d записей (project_id=%s)",
            len(self._entries),
            self._project_id,
        )

    # ------------------------------------------------------------------
    # KnowledgeBaseProvider Protocol
    # ------------------------------------------------------------------

    def search_by_error(
        self,
        error_text: str,
        *,
        query_label: str | None = None,
        feedback_error_text: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки.

        Если feedback_store задан и feedback_error_text передан, загружает
        все feedback-записи для KB-entries и передаёт в matcher для fuzzy
        matching (TF-IDF cosine similarity).

        Args:
            error_text: Текст ошибки для поиска (message + trace/log).
            query_label: Метка для логирования.
            feedback_error_text: Текст для fuzzy feedback matching
                (message + log, без trace). Если None — feedback не применяется.
        """
        feedback_records = None

        if self._feedback_store is not None and feedback_error_text:
            entry_ids = {
                e.entry_id for e in self._entries
                if e.entry_id is not None
            }
            if entry_ids:
                feedback_records = self._feedback_store.get_feedback_for_entries(
                    entry_ids,
                )
                if feedback_records:
                    logger.debug(
                        "PostgresKB: loaded %d feedback records for %d entries",
                        len(feedback_records), len(entry_ids),
                    )

        return self._matcher.match(
            error_text,
            self._entries,
            query_label=query_label,
            feedback_records=feedback_records,
            feedback_error_text=feedback_error_text,
        )

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все загруженные записи."""
        return list(self._entries)

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        return self._entries_by_id.get(entry_id)
