"""Модуль Gradle/Maven, которому принадлежит тест.

В многомодульном Java/Kotlin-проекте база знаний ведётся по модулям
(``<модуль>/alla-kb``): падения разных модулей с одинаковым текстом ошибки не
должны узнавать записи друг друга. Модуль кластера — модуль исходника теста
(по ``full_name``), а не кадров стека: стек может вести в общий ``core``-модуль.

Модулем считается ближайшая папка выше исходника с ``build.gradle``,
``build.gradle.kts`` или ``pom.xml``; сам корень проекта не в счёт. Так не
нужно разбирать ``settings.gradle`` и ``<modules>``, а вложенные модули
(``services/orders/api``) находятся так же. Нет такой папки — модуль ``""``
(корень проекта, не Java/Kotlin).

Проект считается многомодульным, только если исходники лежат минимум в двух
модулях. Один проект во вложенной папке (``autotests/pom.xml`` в репозитории,
где скилл стоит в корне) — это одномодульный проект: его база остаётся в корне,
иначе после обновления скилл потерял бы прежние записи и историю повторов.

Совпадение ``full_name`` с исходником должно быть однозначным: одинаковый
класс в двух модулях (``ru.company.SmokeTest``) модуль не определяет, иначе
запись базы знаний попала бы в чужой модуль.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePath

from alla_skill_lib.code_hints import ProjectIndex, sources_of_test

BUILD_MARKERS = ("build.gradle", "build.gradle.kts", "pom.xml")
# Не ``.kts``: это ещё и скрипты сборки (``build.gradle.kts``), а не тесты.
JVM_EXTENSIONS = frozenset({".java", ".kt", ".groovy", ".scala"})
# Код самой сборки (конвенции Gradle) модулем проекта не считается.
BUILD_SUPPORT_DIRS = frozenset({"buildSrc", "build-logic", "buildLogic"})
MAX_PROBED_TESTS = 50  # сколько участников кластера смотреть, чтобы выбрать модуль


@dataclass(frozen=True)
class ClusterModule:
    """Модуль кластера: определён (``module``) или нет; ``candidates`` — неоднозначные совпадения."""

    module: str | None = None
    candidates: frozenset[str] = frozenset()


class ModuleResolver:
    """Определяет модуль по исходнику; результат по папке запоминается."""

    def __init__(self, root: Path, index: ProjectIndex) -> None:
        self.root = root.resolve()
        self.index = index
        self._by_dir: dict[PurePath, str] = {}
        self._multimodule: bool | None = None

    @property
    def is_multimodule(self) -> bool:
        """Исходники проекта лежат минимум в двух модулях (корень проекта — тоже модуль)."""
        if self._multimodule is None:
            found: set[str] = set()
            for source in self.index.source_paths():
                if source.suffix in JVM_EXTENSIONS and not BUILD_SUPPORT_DIRS.intersection(source.parts):
                    found.add(self._module_of_dir(source.parent))
                    if len(found) > 1:
                        break
            self._multimodule = len(found) > 1
        return self._multimodule

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

    def cluster_module(self, full_names: Iterable[str]) -> ClusterModule:
        """Самый частый модуль среди тестов кластера, чей исходник найден однозначно.

        При равенстве выигрывает первый встреченный: вызывающий ставит представителя
        кластера первым. Тест без исходника или с неоднозначным исходником не голосует;
        модули неоднозначных совпадений попадают в ``candidates``. В одномодульном
        проекте модуль всегда ``""``.
        """
        if not self.is_multimodule:
            return ClusterModule("")
        votes: Counter[str] = Counter()
        candidates: set[str] = set()
        for full_name in islice(full_names, MAX_PROBED_TESTS):
            modules = {self.module_of(source) for source in sources_of_test(self.index, full_name)}
            if len(modules) == 1:
                votes[next(iter(modules))] += 1
            else:
                candidates |= modules
        if votes:
            return ClusterModule(votes.most_common(1)[0][0])
        return ClusterModule(None, frozenset(candidates))


def resolve_run_modules(found: Mapping[str, ClusterModule]) -> dict[str, str]:
    """Модули кластеров прогона; неопределённый берёт модуль прогона, иначе корень.

    Модуль прогона есть, когда все кластеры с определённым модулем относятся к
    одному и тому же модулю. Если модулей два и больше (или ни одного), кластер
    без модуля получает ``""``: записи чужого модуля ему подсказывать нельзя.
    Кластер с неоднозначным исходником берёт модуль прогона, только если тот
    среди кандидатов; иначе общая база корня.
    """
    determined = {cluster.module for cluster in found.values() if cluster.module is not None}
    run_module = next(iter(determined)) if len(determined) == 1 else ""
    result: dict[str, str] = {}
    for key, cluster in found.items():
        if cluster.module is not None:
            result[key] = cluster.module
        elif cluster.candidates and run_module not in cluster.candidates:
            result[key] = ""
        else:
            result[key] = run_module
    return result
