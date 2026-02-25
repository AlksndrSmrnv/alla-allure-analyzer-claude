"""PostgreSQL-реализация базы знаний.

Загружает все записи из PostgreSQL при инициализации (load-at-init-time),
кэширует их в памяти и делегирует поиск существующему TextMatcher.
Точный аналог YamlKnowledgeBase по контракту и поведению.
"""

from __future__ import annotations

import logging

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.matcher import MatcherConfig, TextMatcher
from alla.knowledge.models import KBEntry, KBMatchResult, RootCauseCategory

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
    ) -> None:
        """
        Args:
            dsn: Строка подключения PostgreSQL в формате libpq / URI.
                 Пример: "postgresql://user:pass@host:5432/dbname"
            matcher_config: Конфигурация TextMatcher. None → defaults.
            project_id: ID проекта Allure TestOps. Если задан, загружаются
                        глобальные записи (project_id IS NULL) + записи этого
                        проекта (project_id = N). Если None — только глобальные.
        """
        self._dsn = dsn
        self._project_id = project_id
        self._matcher = TextMatcher(config=matcher_config)
        self._entries: list[KBEntry] = []
        self._entries_by_id: dict[str, KBEntry] = {}
        self._load()

    def _load(self) -> None:
        """Загрузить записи из PostgreSQL в память.

        Raises:
            KnowledgeBaseError: При недоступности psycopg, ошибке подключения
                или ошибке запроса.
        """
        try:
            import psycopg  # deferred: psycopg[binary] — опциональная зависимость
        except ImportError as exc:
            raise KnowledgeBaseError(
                "psycopg не установлен. Установите: pip install 'alla[postgres]'"
            ) from exc

        if self._project_id is not None:
            query = """
                SELECT id, title, description, error_example,
                       category, resolution_steps
                FROM alla.kb_entry
                WHERE project_id IS NULL OR project_id = %s
                ORDER BY project_id NULLS FIRST, id
            """
            params: tuple = (self._project_id,)
        else:
            query = """
                SELECT id, title, description, error_example,
                       category, resolution_steps
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
            entry_id, title, description, error_example, category_raw, resolution_steps = row
            try:
                entry = KBEntry(
                    id=entry_id,
                    title=title,
                    description=description or "",
                    error_example=error_example,
                    category=RootCauseCategory(category_raw),
                    resolution_steps=list(resolution_steps or []),
                )
            except Exception as exc:
                logger.warning(
                    "PostgresKB: ошибка валидации записи id=%r: %s. Пропущена.",
                    entry_id, exc,
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
            logger.debug("PostgresKB: загружена запись id=%r", entry.id)

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
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки."""
        return self._matcher.match(
            error_text,
            self._entries,
            query_label=query_label,
        )

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все загруженные записи."""
        return list(self._entries)

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        return self._entries_by_id.get(entry_id)
