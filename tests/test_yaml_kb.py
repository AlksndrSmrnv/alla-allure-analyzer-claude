"""Тесты YamlKnowledgeBase: загрузка YAML, поиск по ID, поиск по ошибке."""

from __future__ import annotations

from alla.knowledge.yaml_kb import YamlKnowledgeBase

_YAML_TWO_ENTRIES = """\
- id: entry_one
  title: "First Entry"
  description: "First test entry"
  error_pattern: "NullPointerException"
  category: "service"
  resolution_steps:
    - "Fix null check"

- id: entry_two
  title: "Second Entry"
  description: "Second test entry"
  error_pattern: "UnknownHostException"
  category: "env"
  resolution_steps:
    - "Check DNS servers"
"""


def test_loads_entries_from_yaml_directory(tmp_path) -> None:
    """YAML-файл с двумя записями загружается корректно."""
    yaml_file = tmp_path / "entries.yaml"
    yaml_file.write_text(_YAML_TWO_ENTRIES, encoding="utf-8")

    kb = YamlKnowledgeBase(tmp_path)

    entries = kb.get_all_entries()
    assert len(entries) == 2
    assert {e.id for e in entries} == {"entry_one", "entry_two"}


def test_missing_directory_results_in_empty_kb(tmp_path) -> None:
    """Несуществующая директория -> пустая KB без исключений."""
    kb = YamlKnowledgeBase(tmp_path / "nonexistent")

    assert kb.get_all_entries() == []


def test_get_entry_by_id(tmp_path) -> None:
    """Поиск по ID возвращает правильную запись; несуществующий ID -> None."""
    yaml_file = tmp_path / "entries.yaml"
    yaml_file.write_text(_YAML_TWO_ENTRIES, encoding="utf-8")
    kb = YamlKnowledgeBase(tmp_path)

    entry = kb.get_entry_by_id("entry_one")
    assert entry is not None
    assert entry.title == "First Entry"

    assert kb.get_entry_by_id("nonexistent") is None


def test_search_by_error_finds_relevant_entry(tmp_path) -> None:
    """search_by_error находит запись по совпадению подстроки error_pattern."""
    yaml_file = tmp_path / "entries.yaml"
    yaml_file.write_text(_YAML_TWO_ENTRIES, encoding="utf-8")
    kb = YamlKnowledgeBase(tmp_path)

    results = kb.search_by_error(
        "ERROR java.net.UnknownHostException: host not found",
    )

    assert len(results) >= 1
    entry_ids = [r.entry.id for r in results]
    assert "entry_two" in entry_ids


def test_search_by_error_is_case_insensitive(tmp_path) -> None:
    """Матчинг в search_by_error регистронезависимый."""
    yaml_file = tmp_path / "entries.yaml"
    yaml_file.write_text(_YAML_TWO_ENTRIES, encoding="utf-8")
    kb = YamlKnowledgeBase(tmp_path)

    results = kb.search_by_error(
        "java.lang.nullpointerexception: value is null",
    )

    assert len(results) >= 1
    entry_ids = [r.entry.id for r in results]
    assert "entry_one" in entry_ids
