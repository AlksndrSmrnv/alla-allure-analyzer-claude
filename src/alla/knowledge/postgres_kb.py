"""PostgreSQL-реализация базы знаний."""

from __future__ import annotations

import logging

import psycopg  # psycopg[binary] — обязательная зависимость (см. pyproject.toml)

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
            KnowledgeBaseError: При ошибке подключения или ошибке запроса.
        """
        if self._project_id is not None:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       step_path, category, resolution_steps, project_id
                FROM alla.kb_entry
                WHERE project_id IS NULL
                   OR project_id = %s
                   OR project_id IN (
                        SELECT pg2.project_id
                        FROM alla.project_group pg1
                        JOIN alla.project_group pg2
                          ON pg1.group_id = pg2.group_id
                        WHERE pg1.project_id = %s
                      )
                ORDER BY
                    CASE
                        WHEN project_id IS NULL THEN 0   -- глобальные первыми
                        WHEN project_id = %s     THEN 2  -- текущий проект последним (побеждает)
                        ELSE 1                            -- sibling-проекты посередине
                    END,
                    id,
                    project_id
            """
            params: tuple = (self._project_id, self._project_id, self._project_id)
        else:
            query = """
                SELECT entry_id, id, title, description, error_example,
                       step_path, category, resolution_steps, project_id
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
                step_path,
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
                    step_path=step_path,
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
                # Порядок загрузки: global → sibling → current project.
                # Более приоритетная запись переопределяет менее приоритетную.
                prev = self._entries_by_id[entry.id]
                logger.debug(
                    "PostgresKB: id=%r переопределена: project_id=%s → project_id=%s.",
                    entry.id, prev.project_id, entry.project_id,
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
        query_step_path: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки.

        Args:
            error_text: Текст ошибки для поиска (message + trace/log).
            query_label: Метка для логирования.
            query_step_path: Путь шага текущего кластера.
        """
        return self._matcher.match(
            error_text,
            self._entries,
            query_label=query_label,
            query_step_path=query_step_path,
        )

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все загруженные записи."""
        return list(self._entries)

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        return self._entries_by_id.get(entry_id)
