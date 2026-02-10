"""Файловая реализация базы знаний на основе YAML."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.matcher import MatcherConfig, TextMatcher
from alla.knowledge.models import KBEntry, KBMatchResult

logger = logging.getLogger(__name__)


class YamlKnowledgeBase:
    """Реализация KnowledgeBaseProvider, читающая YAML-файлы с диска.

    Загружает все .yaml/.yml файлы из указанной директории (рекурсивно).
    Каждый файл может содержать один YAML-документ (dict) или список (list[dict]).
    Все записи загружаются в память при инициализации.

    Реализует Protocol KnowledgeBaseProvider.
    """

    def __init__(
        self,
        kb_path: str | Path,
        matcher_config: MatcherConfig | None = None,
    ) -> None:
        self._kb_path = Path(kb_path)
        self._matcher = TextMatcher(matcher_config)
        self._entries: list[KBEntry] = []
        self._entries_by_id: dict[str, KBEntry] = {}
        self._load()

    def _load(self) -> None:
        """Загрузить все YAML-файлы из директории KB.

        Raises:
            KnowledgeBaseError: Если путь существует, но не является директорией,
                или если нет прав доступа к директории.
        """
        if not self._kb_path.exists():
            logger.warning(
                "Директория базы знаний не найдена: %s. KB будет пустой.",
                self._kb_path,
            )
            return

        if not self._kb_path.is_dir():
            raise KnowledgeBaseError(
                f"Путь к базе знаний не является директорией: {self._kb_path}"
            )

        try:
            yaml_files = sorted(
                p for p in self._kb_path.rglob("*")
                if p.suffix in (".yaml", ".yml") and p.is_file()
            )
        except PermissionError as exc:
            raise KnowledgeBaseError(
                f"Нет прав доступа к директории базы знаний: {self._kb_path}"
            ) from exc

        for path in yaml_files:
            try:
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except PermissionError as exc:
                raise KnowledgeBaseError(
                    f"Нет прав доступа к KB-файлу: {path}"
                ) from exc
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("Ошибка чтения KB-файла %s: %s. Пропущен.", path, exc)
                continue

            if data is None:
                continue

            # Файл содержит список записей или одну запись
            items = data if isinstance(data, list) else [data]
            for item in items:
                try:
                    entry = KBEntry.model_validate(item)
                    if entry.id in self._entries_by_id:
                        logger.warning(
                            "Дублирующийся ID '%s' в %s. "
                            "Запись пропущена (оставлена первая).",
                            entry.id, path,
                        )
                        continue
                    self._entries.append(entry)
                    self._entries_by_id[entry.id] = entry
                    logger.debug("Загружена KB-запись: %s из %s", entry.id, path)
                except Exception as exc:
                    logger.warning(
                        "Ошибка валидации KB-записи в %s: %s. Запись пропущена.",
                        path, exc,
                    )

        logger.info(
            "База знаний загружена: %d записей из %s",
            len(self._entries),
            self._kb_path,
        )

    def search_by_failure(
        self,
        status_message: str | None,
        status_trace: str | None,
        category: str | None,
        *,
        status_log: str | None = None,
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные ошибке."""
        return self._matcher.match(
            query_message=status_message,
            query_trace=status_trace,
            query_category=category,
            query_log=status_log,
            entries=self._entries,
        )

    def search_by_text(
        self,
        query_text: str,
        *,
        max_results: int = 5,
        min_score: float = 0.0,
    ) -> list[KBMatchResult]:
        """Поиск по произвольному тексту."""
        return self._matcher.match(
            query_message=query_text,
            query_trace=None,
            query_category=None,
            entries=self._entries,
            min_score=min_score,
            max_results=max_results,
        )

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все записи."""
        return list(self._entries)

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        return self._entries_by_id.get(entry_id)
