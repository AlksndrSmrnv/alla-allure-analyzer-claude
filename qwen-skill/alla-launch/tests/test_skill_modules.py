"""Модули Gradle/Maven и база знаний по модулям: определение модуля, каталог ``alla-kb``."""

from __future__ import annotations

from pathlib import Path, PurePath

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_skill_lib.code_hints import ProjectIndex
from alla_skill_lib.history import recurrence, run_records
from alla_skill_lib.kb import discover_kb_dirs, kb_dir_for, kb_label
from alla_skill_lib.modules import ClusterModule, ModuleResolver, resolve_run_modules


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _resolver(root: Path) -> ModuleResolver:
    return ModuleResolver(root, ProjectIndex(root))


def _multimodule(root: Path) -> None:
    _touch(root / "settings.gradle.kts")
    _touch(root / "build.gradle.kts")  # сборка корня модулем не считается
    _touch(root / "orders" / "build.gradle.kts")
    _touch(root / "orders" / "src" / "test" / "java" / "ru" / "a" / "AlphaTest.java")
    _touch(root / "orders" / "src" / "test" / "kotlin" / "ru" / "a" / "BetaTest.kt")
    _touch(root / "auth" / "pom.xml")
    _touch(root / "auth" / "src" / "test" / "java" / "ru" / "b" / "GammaTest.java")


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("orders/src/test/java/ru/a/AlphaTest.java", "orders"),  # Gradle Kotlin DSL
        ("orders/src/test/kotlin/ru/a/BetaTest.kt", "orders"),
        ("auth/src/test/java/ru/b/GammaTest.java", "auth"),  # Maven
        ("services/payments/api/src/test/java/PayTest.java", "services/payments/api"),  # ближайший
        ("src/test/java/RootTest.java", ""),  # исходник корневого проекта
        ("orders/tests/test_login.py", ""),  # не Java/Kotlin — модулем не считается
    ],
)
def test_module_is_the_nearest_folder_with_a_build_file(
    tmp_path: Path, source: str, module: str
) -> None:
    _multimodule(tmp_path)
    _touch(tmp_path / "services" / "payments" / "build.gradle")
    _touch(tmp_path / "services" / "payments" / "api" / "build.gradle.kts")

    assert _resolver(tmp_path).module_of(PurePath(source)) == module


def test_project_without_build_files_has_no_modules(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "test" / "java" / "ru" / "a" / "AlphaTest.java")

    assert _resolver(tmp_path).module_of(PurePath("src/test/java/ru/a/AlphaTest.java")) == ""


def test_cluster_module_is_the_most_common_one_and_representative_wins_ties(tmp_path: Path) -> None:
    _multimodule(tmp_path)
    resolver = _resolver(tmp_path)

    names = ["ru.a.AlphaTest.x", "ru.b.GammaTest.y", "ru.a.BetaTest.z"]
    assert resolver.cluster_module(names).module == "orders"
    # Ничья 1:1 — первым стоит представитель кластера.
    assert resolver.cluster_module(["ru.b.GammaTest.y", "ru.a.AlphaTest.x"]).module == "auth"
    assert resolver.cluster_module(iter(["ru.a.AlphaTest.x", "ru.b.GammaTest.y"])).module == "orders"


def test_cluster_module_is_unknown_when_no_source_is_found(tmp_path: Path) -> None:
    _multimodule(tmp_path)

    names = ["Scenario: user logs in", "ru.zzz.Missing.test", ""]
    assert _resolver(tmp_path).cluster_module(names) == ClusterModule(None, frozenset())


def test_cluster_with_tests_of_the_root_project_has_the_root_module(tmp_path: Path) -> None:
    _multimodule(tmp_path)
    _touch(tmp_path / "src" / "test" / "java" / "ru" / "c" / "RootTest.java")

    assert _resolver(tmp_path).cluster_module(["ru.c.RootTest.t"]).module == ""


def _two_smoke_modules(root: Path) -> None:
    """Одинаковый класс ``ru.company.SmokeTest`` в двух модулях и в одном (java + kotlin)."""
    _touch(root / "web" / "pom.xml")
    _touch(root / "mobile" / "build.gradle.kts")
    _touch(root / "web" / "src" / "test" / "java" / "ru" / "company" / "SmokeTest.java")
    _touch(root / "web" / "src" / "test" / "kotlin" / "ru" / "company" / "SmokeTest.kt")
    _touch(root / "mobile" / "src" / "test" / "java" / "ru" / "company" / "SmokeTest.java")
    _touch(root / "mobile" / "src" / "test" / "java" / "ru" / "company" / "OnlyMobileTest.java")


def test_same_class_in_two_modules_does_not_pick_a_module(tmp_path: Path) -> None:
    _two_smoke_modules(tmp_path)

    found = _resolver(tmp_path).cluster_module(["ru.company.SmokeTest.run"])

    # Выбор «первого найденного» отправил бы запись базы знаний в случайный модуль.
    assert found == ClusterModule(None, frozenset({"web", "mobile"}))


def test_ambiguous_tests_do_not_vote_but_unambiguous_ones_do(tmp_path: Path) -> None:
    _two_smoke_modules(tmp_path)

    names = ["ru.company.SmokeTest.run", "ru.company.OnlyMobileTest.t"]
    assert _resolver(tmp_path).cluster_module(names) == ClusterModule("mobile")


def test_same_class_twice_in_one_module_is_not_ambiguous(tmp_path: Path) -> None:
    _two_smoke_modules(tmp_path)
    (tmp_path / "mobile" / "src" / "test" / "java" / "ru" / "company" / "SmokeTest.java").unlink()

    assert _resolver(tmp_path).cluster_module(["ru.company.SmokeTest.run"]) == ClusterModule("web")


def test_single_project_in_a_nested_folder_is_not_multimodule(tmp_path: Path) -> None:
    # Репозиторий с единственным проектом ``autotests/pom.xml`` и скиллом в корне.
    _touch(tmp_path / "autotests" / "pom.xml")
    _touch(tmp_path / "autotests" / "src" / "test" / "java" / "ru" / "a" / "AlphaTest.java")
    resolver = _resolver(tmp_path)

    assert not resolver.is_multimodule
    assert resolver.module_of(PurePath("autotests/src/test/java/ru/a/AlphaTest.java")) == "autotests"
    assert resolver.cluster_module(["ru.a.AlphaTest.x"]) == ClusterModule("")  # база остаётся в корне


def test_build_scripts_and_build_logic_do_not_make_a_project_multimodule(tmp_path: Path) -> None:
    _touch(tmp_path / "build.gradle.kts")
    _touch(tmp_path / "settings.gradle.kts")  # ``.kts`` — скрипты сборки, а не тесты
    _touch(tmp_path / "buildSrc" / "build.gradle.kts")
    _touch(tmp_path / "buildSrc" / "src" / "main" / "kotlin" / "Conventions.kt")
    _touch(tmp_path / "build-logic" / "pom.xml")
    _touch(tmp_path / "build-logic" / "src" / "main" / "java" / "Plugin.java")
    _touch(tmp_path / "src" / "test" / "java" / "ru" / "a" / "AlphaTest.java")

    assert not _resolver(tmp_path).is_multimodule


def test_two_modules_with_sources_make_a_project_multimodule(tmp_path: Path) -> None:
    _multimodule(tmp_path)

    assert _resolver(tmp_path).is_multimodule


def test_root_sources_next_to_a_module_make_a_project_multimodule(tmp_path: Path) -> None:
    _touch(tmp_path / "pom.xml")
    _touch(tmp_path / "src" / "test" / "java" / "ru" / "c" / "RootTest.java")
    _touch(tmp_path / "e2e" / "pom.xml")
    _touch(tmp_path / "e2e" / "src" / "test" / "java" / "ru" / "e" / "E2eTest.java")
    resolver = _resolver(tmp_path)

    assert resolver.is_multimodule
    assert resolver.cluster_module(["ru.e.E2eTest.t"]).module == "e2e"
    assert resolver.cluster_module(["ru.c.RootTest.t"]).module == ""


def _found(**clusters: str | tuple[str, ...] | None) -> dict[str, ClusterModule]:
    """``name=None`` — не найден, ``name="m"`` — определён, ``name=("a", "b")`` — неоднозначен."""
    return {
        name: ClusterModule(None, frozenset(value)) if isinstance(value, tuple) else ClusterModule(value)
        for name, value in clusters.items()
    }


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        # Один определившийся модуль — это модуль прогона.
        ({"01": "orders", "02": None}, {"01": "orders", "02": "orders"}),
        ({"01": "orders", "02": None, "03": "orders"}, {"01": "orders", "02": "orders", "03": "orders"}),
        # Два модуля — безымянному остаётся общая база корня.
        ({"01": "orders", "02": "auth", "03": None}, {"01": "orders", "02": "auth", "03": ""}),
        ({"01": "orders", "02": "", "03": None}, {"01": "orders", "02": "", "03": ""}),
        ({"01": None, "02": None}, {"01": "", "02": ""}),  # ни одного определившегося
        ({"01": "", "02": None}, {"01": "", "02": ""}),  # одномодульный проект
        # Неоднозначный кластер берёт модуль прогона, только если тот среди кандидатов.
        ({"01": "web", "02": ("web", "mobile")}, {"01": "web", "02": "web"}),
        ({"01": "api", "02": ("web", "mobile")}, {"01": "api", "02": ""}),
        ({"01": "web", "02": "mobile", "03": ("web", "mobile")}, {"01": "web", "02": "mobile", "03": ""}),
        ({"01": ("web", "mobile")}, {"01": ""}),
        ({}, {}),
    ],
)
def test_unknown_module_takes_the_run_module_otherwise_the_root(
    found: dict[str, str | tuple[str, ...] | None], expected: dict[str, str]
) -> None:
    assert resolve_run_modules(_found(**found)) == expected


def test_kb_dir_is_in_the_root_or_in_the_module(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "tests"
    nested.mkdir()

    assert kb_dir_for(tmp_path) == tmp_path / "alla-kb"
    assert kb_dir_for(tmp_path, "orders") == tmp_path / "orders" / "alla-kb"
    assert kb_dir_for(tmp_path, "services/orders/api") == tmp_path / "services/orders/api/alla-kb"
    # Проект внутри репозитория: общая база — в корне git, модульная — рядом с модулем.
    assert kb_dir_for(nested) == tmp_path / "alla-kb"
    assert kb_dir_for(nested, "orders") == nested / "orders" / "alla-kb"


def test_kb_label_is_a_path_from_the_project_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = tmp_path / "tests"
    project.mkdir()

    assert kb_label(tmp_path, tmp_path / "alla-kb") == "alla-kb"
    assert kb_label(tmp_path, tmp_path / "orders" / "alla-kb") == "orders/alla-kb"
    assert kb_label(project, tmp_path / "alla-kb") == "alla-kb"  # git-корень выше проекта


def test_discover_kb_dirs_skips_service_and_build_folders(tmp_path: Path) -> None:
    for folder in ("alla-kb", "orders/alla-kb", "services/api/alla-kb", "node_modules/x/alla-kb",
                   "orders/build/alla-kb", ".git/alla-kb", "alla-kb/inner/alla-kb"):
        (tmp_path / folder).mkdir(parents=True)

    found = [path.relative_to(tmp_path).as_posix() for path in discover_kb_dirs(tmp_path)]

    assert found == ["alla-kb", "orders/alla-kb", "services/api/alla-kb"]


def test_history_recurrence_is_per_module() -> None:
    history = [
        {"date": "2026-09-20", "launch_id": 1, "signature": "v7:a", "module": "orders"},
        {"date": "2026-09-21", "launch_id": 2, "signature": "v7:a", "module": "auth"},
        {"date": "2026-09-22", "launch_id": 3, "signature": "v7:a", "module": ""},  # корень
        {"date": "2026-09-23", "launch_id": 4, "kb_entry": "kb_1", "module": "orders"},
    ]

    orders = recurrence(history, launch_id=9, signature="v7:a", kb_ids={"kb_1"}, module="orders")
    assert orders == {"launches": 2, "first_date": "2026-09-20", "last_date": "2026-09-23"}
    auth = recurrence(history, launch_id=9, signature="v7:a", kb_ids=set(), module="auth")
    assert auth == {"launches": 1, "first_date": "2026-09-21", "last_date": "2026-09-21"}
    root = recurrence(history, launch_id=9, signature="v7:a", kb_ids=set())
    assert root == {"launches": 1, "first_date": "2026-09-22", "last_date": "2026-09-22"}
    assert recurrence(history, launch_id=9, signature="v7:a", kb_ids=set(), module="other") is None


def test_history_records_carry_the_module() -> None:
    run = {
        "launch_id": 5, "created_at": "2026-09-30T10:00:00",
        "clusters": [
            {"file_id": "01", "signature": "v7:a", "label": "x", "member_count": 1, "module": "orders"},
            {"file_id": "02", "signature": "v7:b", "label": "y", "member_count": 1, "module": ""},
        ],
    }

    records = run_records(run, {}, set(), "5-run")

    assert [record["module"] for record in records] == ["orders", ""]
