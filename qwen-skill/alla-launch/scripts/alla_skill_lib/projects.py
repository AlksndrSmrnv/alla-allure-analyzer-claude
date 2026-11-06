"""Проект TestOps прогона для ``prepare``: из ``ALLURE_PROJECT_ID`` и ``--module``/``--project``.

Поиск результатов MCP TestOps ищет в одном проекте (``projectId``), а номер прогона и ссылка
``…/launch/<id>`` проекта не называют. Один проект в настройках берётся сам. В многомодульном
проекте автотестов у модулей свои проекты (``orders=5,payments/api=7``): какой нужен, решает
пользователь — ``prepare <id> --module <модуль>``; угадывать перебором скилл не пытается.

Модуль здесь — только имя в настройках: на определение модуля кластера по исходникам и на
базу знаний (``modules.py``) выбор не влияет.
"""

from __future__ import annotations

from dataclasses import dataclass

from alla_core.config import ProjectRef, Settings
from alla_core.exceptions import ConfigurationError


@dataclass(frozen=True)
class ProjectChoice:
    """Проект запросов (``None`` — без проекта) и модуль из настроек (``None`` — не назван)."""

    id: int | None
    module: str | None = None


class ProjectNotChosen(ConfigurationError):
    """В настройках несколько проектов, а модуль не назван (или назван неизвестный)."""

    def __init__(self, projects: tuple[ProjectRef, ...], unknown: str | None = None) -> None:
        self.projects = projects
        self.unknown = unknown
        super().__init__(
            f"модуля «{unknown}» нет в ALLURE_PROJECT_ID" if unknown is not None
            else "в ALLURE_PROJECT_ID несколько проектов TestOps, модуль не назван"
        )


def choose_project(
    settings: Settings, *, module: str | None = None, project: int | None = None,
) -> ProjectChoice:
    """Проект прогона.

    ``project`` — он (модуль — названный или единственный модуль с этим номером в настройках);
    ``module`` — проект этого модуля (точное имя, иначе единственный модуль с таким последним
    сегментом пути: «orders» для ``services/orders``). Без них: проектов нет — без проекта,
    один — он, несколько — ``ProjectNotChosen``. Один номер без модуля (``ALLURE_PROJECT_ID=5``)
    подходит любому модулю.
    """
    projects = settings.projects
    named = _normalize(module) if module is not None else None
    if project is not None:
        if named is None:
            owners = [item.module for item in projects if item.id == project and item.module]
            named = owners[0] if len(owners) == 1 else None
        return ProjectChoice(project, named or None)
    modular = [item for item in projects if item.module]
    if named is not None and modular:
        found = _find(modular, named)
        if found is None:
            raise ProjectNotChosen(projects, unknown=named)
        return ProjectChoice(found.id, found.module)
    if not projects:
        return ProjectChoice(None)
    if len(projects) == 1:
        return ProjectChoice(projects[0].id, projects[0].module or None)
    raise ProjectNotChosen(projects)


def _normalize(module: str) -> str:
    return module.strip().replace("\\", "/").strip("/")


def _find(projects: list[ProjectRef], module: str) -> ProjectRef | None:
    exact = [item for item in projects if item.module == module]
    if exact:
        return exact[0]
    tail = [item for item in projects if item.module.rsplit("/", 1)[-1] == module]
    return tail[0] if len(tail) == 1 else None
