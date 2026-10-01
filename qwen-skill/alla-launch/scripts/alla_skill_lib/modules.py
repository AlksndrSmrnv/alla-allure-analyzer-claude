"""Модуль Gradle/Maven, которому принадлежит тест.

В многомодульном Java/Kotlin-проекте база знаний ведётся по модулям
(``<модуль>/alla-kb``): падения разных модулей с одинаковым текстом ошибки не
должны узнавать записи друг друга. Модуль кластера — модуль исходника теста
(по ``full_name``), а не кадров стека: стек может вести в общий ``core``-модуль.

Модулем считается ближайшая папка выше исходника с ``build.gradle``,
``build.gradle.kts`` или ``pom.xml``; сам корень проекта не в счёт. Так не
нужно разбирать ``settings.gradle`` и ``<modules>``, а вложенные модули
(``services/orders/api``) находятся так же. Нет такой папки — модуль ``""``
(корень проекта, одномодульный проект, не Java/Kotlin).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from itertools import islice
from pathlib import Path, PurePath

from alla_skill_lib.code_hints import ProjectIndex, source_of_test

BUILD_MARKERS = ("build.gradle", "build.gradle.kts", "pom.xml")
JVM_EXTENSIONS = frozenset({".java", ".kt", ".kts", ".groovy", ".scala"})
MAX_PROBED_TESTS = 50  # сколько участников кластера смотреть, чтобы выбрать модуль


class ModuleResolver:
    """Определяет модуль по исходнику; результат по папке запоминается."""

    def __init__(self, root: Path, index: ProjectIndex) -> None:
        self.root = root.resolve()
        self.index = index
        self._by_dir: dict[PurePath, str] = {}

    def module_of(self, source: PurePath) -> str:
        """Путь модуля от корня проекта в виде ``a/b`` (``""`` — корень)."""
        if source.suffix not in JVM_EXTENSIONS:
            return ""
        return self._module_of_dir(source.parent)

    def _module_of_dir(self, directory: PurePath) -> str:
        if not directory.parts:  # дошли до корня проекта
            return ""
        cached = self._by_dir.get(directory)
        if cached is None:
            if any((self.root / directory / marker).is_file() for marker in BUILD_MARKERS):
                cached = directory.as_posix()
            else:
                cached = self._module_of_dir(directory.parent)
            self._by_dir[directory] = cached
        return cached

    def cluster_module(self, full_names: Iterable[str]) -> str | None:
        """Самый частый модуль среди тестов кластера (``None`` — ни один исходник не найден).

        При равенстве выигрывает первый встреченный: вызывающий ставит представителя
        кластера первым.
        """
        counts: Counter[str] = Counter()
        for full_name in islice(full_names, MAX_PROBED_TESTS):
            source = source_of_test(self.index, full_name)
            if source is not None:
                counts[self.module_of(source)] += 1
        return counts.most_common(1)[0][0] if counts else None


def resolve_run_modules(modules: Mapping[str, str | None]) -> dict[str, str]:
    """Модули кластеров прогона; неопределённый берёт модуль прогона, иначе корень.

    Модуль прогона есть, когда все кластеры с определённым модулем относятся к
    одному и тому же модулю. Если модулей два и больше (или ни одного), кластер
    без модуля получает ``""``: записи чужого модуля ему подсказывать нельзя.
    """
    found = {module for module in modules.values() if module is not None}
    fallback = next(iter(found)) if len(found) == 1 else ""
    return {key: fallback if module is None else module for key, module in modules.items()}
