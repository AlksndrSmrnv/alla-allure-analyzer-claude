"""Файловая реализация базы знаний на основе YAML."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.matcher import MatcherConfig, TextMatcher
from alla.knowledge.models import KBEntry, KBMatchResult

logger = logging.getLogger(__name__)


_PROJECT_FILE_RE = re.compile(r"^project_\d+\.ya?ml$")


class YamlKnowledgeBase:
    """Реализация KnowledgeBaseProvider, читающая YAML-файлы с диска.

    Загружает .yaml/.yml файлы из указанной директории (рекурсивно) с фильтрацией:
    - Глобальные файлы (не соответствующие ``project_<id>.yaml``) — загружаются всегда.
    - Проектные файлы (``project_<id>.yaml``) — загружается только файл текущего проекта
      (если ``project_id`` задан). Файлы других проектов пропускаются.

    Каждый файл может содержать один YAML-документ (dict) или список (list[dict]).
    Все записи загружаются в память при инициализации.

    Реализует Protocol KnowledgeBaseProvider.
    """

    def __init__(
        self,
        kb_path: str | Path,
        *,
        matcher_config: MatcherConfig | None = None,
        project_id: int | None = None,
    ) -> None:
        self._kb_path = Path(kb_path)
        self._project_id = project_id
        self._matcher = TextMatcher(config=matcher_config)
        self._entries: list[KBEntry] = []
        self._entries_by_id: dict[str, KBEntry] = {}
        self._ensure_project_file()
        self._load()
        if self._entries:
            self._matcher.fit(self._entries)

    def _ensure_project_file(self) -> None:
        """Создать пустой файл KB для проекта, если он ещё не существует."""
        if self._project_id is None:
            return

        project_file = self._kb_path / f"project_{self._project_id}.yaml"
        if project_file.exists():
            return

        if self._kb_path.exists() and not self._kb_path.is_dir():
            # Путь существует, но не является директорией.
            # _load() обнаружит это и выбросит KnowledgeBaseError с понятным сообщением.
            return

        if not self._kb_path.exists():
            self._kb_path.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Создана директория базы знаний: %s", self._kb_path,
            )

        project_file.write_text(
            f"# alla — база знаний для проекта #{self._project_id}\n"
            f"# Формат записей см. в entries.yaml\n"
            f"[]\n",
            encoding="utf-8",
        )
        logger.info(
            "Создан файл базы знаний для проекта #%d: %s",
            self._project_id,
            project_file,
        )

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
            all_yaml = sorted(
                p for p in self._kb_path.rglob("*")
                if p.suffix in (".yaml", ".yml") and p.is_file()
            )
        except PermissionError as exc:
            raise KnowledgeBaseError(
                f"Нет прав доступа к директории базы знаний: {self._kb_path}"
            ) from exc

        # Фильтрация: глобальные файлы + только файл текущего проекта.
        # Файлы других проектов (project_<N>.yaml/.yml, N ≠ self._project_id) пропускаются.
        # Сравниваем по stem (имя без расширения), чтобы корректно обрабатывать
        # оба расширения: project_42.yaml и project_42.yml.
        own_project_stem = (
            f"project_{self._project_id}"
            if self._project_id is not None
            else None
        )
        yaml_files = [
            p for p in all_yaml
            if not _PROJECT_FILE_RE.match(p.name)                    # глобальный файл
            or (own_project_stem is not None and p.stem == own_project_stem)  # свой проект
        ]

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

    def search_by_error(
        self,
        error_text: str,
        *,
        query_label: str | None = None,
        error_fingerprint: str | None = None,  # noqa: ARG002 — YAML backend has no feedback
    ) -> list[KBMatchResult]:
        """Найти записи KB, релевантные тексту ошибки."""
        return self._matcher.match(
            error_text,
            self._entries,
            query_label=query_label,
        )

    def get_all_entries(self) -> list[KBEntry]:
        """Вернуть все записи."""
        return list(self._entries)

    def get_entry_by_id(self, entry_id: str) -> KBEntry | None:
        """Найти запись по ID."""
        return self._entries_by_id.get(entry_id)
