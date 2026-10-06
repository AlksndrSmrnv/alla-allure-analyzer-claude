"""Известные проблемы: подтверждение записи базы знаний и группы в отчёте (шаг 6)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from alla_skill_lib.analysis_format import parse_analysis
from alla_skill_lib.history import run_records
from alla_skill_lib.kb import KBRecord, ProjectKB
from alla_skill_lib.known_issues import KnownIssues, known_issues
from alla_skill_lib.report import (
    MAX_BRIEF_ITEMS,
    _brief_units,
    _problem,
    _sort_key,
    build_summary_data,
    render_report,
)
from alla_skill_lib.workspace import RunPaths
from skill_fixtures import without_libmagic  # noqa: F401
from test_skill_report import APP, ENV, TEST, _run, _section

POOL = "HikariPool-1 - Connection is not available, request timed out"
ENTRY = "payment_db_pool_1a2b3c4d"
CONSISTENCY_DIFFERENT = "СОГЛАСОВАННОСТЬ: разные проблемы — в одном логе пул БД, в другом NPE\n"


def _record(entry_id: str = ENTRY, category: str = "service", **kwargs: Any) -> KBRecord:
    return KBRecord(
        id=entry_id,
        title=kwargs.pop("title", "Пул соединений БД платежей исчерпан"),
        category=category,
        description=kwargs.pop("description", "Сервис платежей не отдаёт соединения из пула БД."),
        resolution_steps=kwargs.pop("steps", ["Увеличить пул payment-db", "Проверить утечки соединений"]),
        error_example=kwargs.pop("error_example", POOL),
        **kwargs,
    )


def _setup(
    tmp_path: Path,
    sizes: list[int],
    texts: list[str],
    *,
    evidence: list[str] | None = None,
    records: list[KBRecord] | None = None,
    kb_dirs: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], RunPaths]:
    """Прогон с базой знаний проекта: у кластера i сигнатура «v6:sig<i>» и evidence/NN.txt."""
    run = _run(sizes)
    project = tmp_path / "project"
    kb_dir = project / "alla-kb"
    run["project_root"] = str(project)
    run["kb_dir"] = str(kb_dir)
    paths = RunPaths(tmp_path / "run")
    for index, entry in enumerate(run["clusters"]):
        entry["signature"] = f"v6:sig{index + 1}"
        entry["kb_dir"] = str(project / kb_dirs[index] / "alla-kb") if kb_dirs and kb_dirs[index] else str(kb_dir)
        entry["module"] = kb_dirs[index] if kb_dirs else ""
        entry["task_format"] = 2
        text = (evidence or [f"error {index + 1}\n{POOL}"] * len(sizes))[index]
        paths.evidence(entry["file_id"]).parent.mkdir(parents=True, exist_ok=True)
        paths.evidence(entry["file_id"]).write_text(text, encoding="utf-8")
    for record in records if records is not None else [_record()]:
        ProjectKB(kb_dir).save(record)
    analyses = {
        entry["file_id"]: parse_analysis(text) for entry, text in zip(run["clusters"], texts)
    }
    return run, analyses, paths


def _kb(text: str, entry_id: str = ENTRY) -> str:
    return text + f"БАЗА ЗНАНИЙ: {entry_id}\n"


def _known(run: dict[str, Any], analyses: dict[str, Any], paths: RunPaths, flagged: set[str] | None = None) -> KnownIssues:
    return known_issues(run, analyses, flagged or set(), paths)


def _render(run: dict[str, Any], analyses: dict[str, Any], paths: RunPaths, known: KnownIssues, **kwargs: Any) -> tuple[str, str]:
    return render_report(run, analyses, kwargs.pop("flagged", set()), "Итог прогона.", paths, known=known, **kwargs)


def _numbers(known: KnownIssues) -> list[list[int]]:
    return [[int(file_id) for file_id in group.file_ids] for group in known.groups]


# --- подтверждение и группы -----------------------------------------------------------


def test_problems_with_one_confirmed_record_form_one_group(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [5, 4, 3, 2], [_kb(APP), _kb(APP), _kb(APP), APP])
    known = _known(run, analyses, paths)
    assert _numbers(known) == [[1, 2, 3]]
    assert known.groups[0].size == 12
    assert set(known.refs) == {"01", "02", "03"}
    assert known.groups[0].record.category == "приложение"
    assert known.groups[0].record.first_step == "Увеличить пул payment-db"


def test_reject_removes_a_problem_from_the_group_and_its_tag(tmp_path: Path) -> None:
    record = _record()
    run, analyses, paths = _setup(tmp_path, [3, 2, 1], [_kb(APP)] * 3, records=[record])
    record.reject("v6:sig2")
    ProjectKB(Path(run["kb_dir"])).save(record)
    known = _known(run, analyses, paths)
    assert _numbers(known) == [[1, 3]]
    assert "02" not in known.refs

    record.reject("v6:sig3")
    ProjectKB(Path(run["kb_dir"])).save(record)
    known = _known(run, analyses, paths)
    assert known.groups == [] and set(known.refs) == {"01"}
    console, full = _render(run, analyses, paths, known)
    assert "[известная проблема: payment_db_pool_1a2b3c4d]" in console
    assert full.count("- Известная проблема:") == 2  # в разделе и в подробностях
    assert "### Известные проблемы из базы знаний" not in full


def test_exact_signature_confirms_without_the_fingerprint_in_evidence(tmp_path: Path) -> None:
    record = _record(confirmed_signatures=["v6:sig1", "v6:sig2"])
    run, analyses, paths = _setup(
        tmp_path, [2, 2], [_kb(APP), _kb(APP)], evidence=["other error", "other error"], records=[record],
    )
    assert _numbers(_known(run, analyses, paths)) == [[1, 2]]


def test_record_that_no_longer_matches_or_is_gone_is_not_confirmed(tmp_path: Path) -> None:
    run, analyses, paths = _setup(
        tmp_path, [2, 2], [_kb(APP), _kb(APP)], evidence=[POOL, "признак исчез"],
    )
    known = _known(run, analyses, paths)
    assert known.groups == [] and set(known.refs) == {"01"}

    (Path(run["kb_dir"]) / f"{ENTRY}.json").unlink()
    assert _known(run, analyses, paths).refs == {}


def test_broken_record_file_is_reported_and_not_confirmed(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [2, 2], [_kb(APP), _kb(APP)])
    (Path(run["kb_dir"]) / f"{ENTRY}.json").write_text("<<<<<<< HEAD\n{}", encoding="utf-8")
    known = _known(run, analyses, paths)
    assert known.refs == {} and known.groups == []
    assert len(known.notes) == 1 and "не читается" in known.notes[0]


def test_unreachable_knowledge_base_keeps_tags_without_groups(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [2, 2], [_kb(APP), _kb(APP)])
    for entry in run["clusters"]:
        entry["kb"] = [{"id": ENTRY, "title": "Пул БД", "category": "приложение", "steps": ["Шаг"]}]
        entry["kb_dir"] = str(tmp_path / "moved" / "alla-kb")
    known = _known(run, analyses, paths)
    assert known.groups == [] and set(known.refs) == {"01", "02"}
    assert not known.refs["01"].verified
    assert len(known.notes) == 1 and "недоступна" in known.notes[0]
    console, _ = _render(run, analyses, paths, known, notes=known.notes)
    assert console.count("[известная проблема: payment_db_pool_1a2b3c4d]") == 2


def test_different_knowledge_base_folders_are_different_records(tmp_path: Path) -> None:
    run, analyses, paths = _setup(
        tmp_path, [2, 2, 1], [_kb(APP)] * 3, kb_dirs=["orders", "payments", "orders"], records=[],
    )
    for module in ("orders", "payments"):
        ProjectKB(tmp_path / "project" / module / "alla-kb").save(_record())
    known = _known(run, analyses, paths)
    assert _numbers(known) == [[1, 3]]
    assert known.several_modules
    assert set(known.refs) == {"01", "02", "03"}
    _, full = _render(run, analyses, paths, known)
    assert "модуль orders" in full


def test_category_that_contradicts_the_record_stays_apart_with_a_mark(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [3, 2, 1], [_kb(APP), _kb(APP), _kb(ENV)])
    known = _known(run, analyses, paths)
    assert _numbers(known) == [[1, 2]]
    assert set(known.mismatched) == {"03"}
    console, full = _render(run, analyses, paths, known)
    assert "[расходится с базой знаний: payment_db_pool_1a2b3c4d]" in console
    assert (
        "- Разбор ссылается на известную проблему payment_db_pool_1a2b3c4d («Пул соединений БД "
        "платежей исчерпан»), но называет другую причину (окружение; в базе знаний — приложение)"
    ) in full


def test_mixed_group_and_flagged_analysis_are_not_grouped(tmp_path: Path) -> None:
    run, analyses, paths = _setup(
        tmp_path, [3, 2, 2, 1],
        [_kb(APP), _kb(APP + CONSISTENCY_DIFFERENT), _kb(APP), _kb(APP)],
    )
    known = _known(run, analyses, paths, flagged={"03"})
    assert _numbers(known) == [[1, 4]]
    assert "02" in known.refs  # метка остаётся, но в группу неоднородная не входит
    assert "03" not in known.refs


def test_tests_are_counted_once(tmp_path: Path) -> None:
    sizes = [5, 4, 3, 2, 1]
    other = _record("login_down_9f8e7d6c", "env", title="Сервис входа недоступен", error_example="auth down")
    run, analyses, paths = _setup(
        tmp_path, sizes,
        [_kb(APP), _kb(ENV, other.id), _kb(APP), _kb(ENV, other.id), APP],
        evidence=[POOL, "auth down", POOL, "auth down", "x"],
        records=[_record(), other],
    )
    known = _known(run, analyses, paths)
    grouped = [file_id for group in known.groups for file_id in group.file_ids]
    assert len(grouped) == len(set(grouped)) == 4
    ungrouped = sum(e["member_count"] for e in run["clusters"] if e["file_id"] not in grouped)
    assert sum(group.size for group in known.groups) + ungrouped == run["counts"]["active_failures"]


# --- отчёт ----------------------------------------------------------------------------


def test_report_lists_known_issue_and_keeps_numbers_and_sections(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [5, 4, 3, 2], [_kb(APP), ENV, _kb(APP), _kb(APP)])
    known = _known(run, analyses, paths)
    console, full = _render(run, analyses, paths, known)
    section = _section(full, "Известные проблемы из базы знаний")
    assert "### Известные проблемы из базы знаний (1)" in section
    assert (
        "- «Пул соединений БД платежей исчерпан» (`payment_db_pool_1a2b3c4d`; возможная ошибка "
        "приложения) — проблемы 1, 3, 4 · 10 тестов"
    ) in section
    assert "   Что делать: Увеличить пул payment-db" in section
    assert full.index("### Коротко") < full.index("### Известные проблемы") < full.index("### Требуют")
    for number in (1, 3, 4):
        assert f"### Проблема {number} —" in full
    assert "- Известная проблема: payment_db_pool_1a2b3c4d — «Пул соединений БД платежей исчерпан», вместе с проблемами 3, 4" in full
    assert "вместе с проблемами 1, 3" in full
    # Счётчики шапки — проблемы, а не группы.
    assert "4 проблемы" in console


def test_brief_collapses_group_into_one_block(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [5, 4, 3, 2], [_kb(APP), ENV, _kb(APP), _kb(APP)])
    known = _known(run, analyses, paths)
    console, _ = _render(run, analyses, paths, known)
    attention = _section(console, "Требуют вашего внимания")
    assert "### 🔴 Требуют вашего внимания (3)" in attention
    assert "**Проблемы 1, 3, 4** · 10 тестов · [известная проблема: payment_db_pool_1a2b3c4d]" in attention
    assert "Одна известная проблема: «Пул соединений БД платежей исчерпан»" in attention
    assert "   База знаний:   [ПРИЛОЖЕНИЕ] Сервис платежей не отдаёт соединения из пула БД." in attention
    assert "   Что делать:    Увеличить пул payment-db" in attention
    assert "   Например:      test_1" in attention
    assert "**Проблема 3**" not in attention and "**Проблема 4**" not in attention
    assert "**Проблема 2**" in _section(console, "Стенд и тестовые данные")


def test_brief_does_not_collapse_problems_with_fixes(tmp_path: Path) -> None:
    from test_skill_report import _proposal

    record = _record("old_status_code_aaaa1111", "test", title="Устаревший код ответа", error_example="status")
    run, analyses, paths = _setup(
        tmp_path, [2, 2], [_kb(TEST, record.id)] * 2, evidence=["status"] * 2, records=[record],
    )
    (tmp_path / "project" / "src").mkdir(parents=True)
    (tmp_path / "project" / "src" / "OrderTest.java").write_text("\n" * 10, encoding="utf-8")
    known = _known(run, analyses, paths)
    assert _numbers(known) == [[1, 2]]
    proposals = {"01": _proposal(), "02": _proposal()}
    console, _ = _render(run, analyses, paths, known, proposals=proposals)
    agent = _section(console, "Агент может поправить сам")
    assert agent.count("**Проблема ") == 2 and "**Проблемы" not in agent


def test_brief_counts_a_collapsed_group_as_one_item(tmp_path: Path) -> None:
    sizes = [10] * 3 + [1] * (MAX_BRIEF_ITEMS + 2)
    texts = [_kb(APP)] * 3 + [APP] * (MAX_BRIEF_ITEMS + 2)
    run, analyses, paths = _setup(tmp_path, sizes, texts)
    known = _known(run, analyses, paths)
    console, _ = _render(run, analyses, paths, known)
    attention = _section(console, "Требуют вашего внимания")
    blocks = re.findall(r"^\*\*Проблем", attention, re.MULTILINE)
    assert len(blocks) == MAX_BRIEF_ITEMS
    # Показаны группа (3 проблемы) и ещё 4 проблемы; хвост — оставшиеся 3 проблемы.
    assert "… и ещё 3 проблемы (3 теста)" in attention


def test_brief_group_numbers_are_limited(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [1] * 8, [_kb(APP)] * 8)
    known = _known(run, analyses, paths)
    console, _ = _render(run, analyses, paths, known)
    assert "**Проблемы 1, 2, 3, 4, 5 и ещё 3** · 8 тестов" in console


def test_brief_size_does_not_grow_with_many_groups(tmp_path: Path) -> None:
    """Свёрнутый блок обрезан по полям, как обычный: потолок брифа не зависит от числа групп."""
    sizes = []
    for groups in (20, 100):
        records = [
            _record(f"known_{index:03d}_abcd1234", title="Очень длинное название записи " * 10,
                    description="Описание " * 100, steps=["Шаг " * 100], error_example=f"marker {index:03d}")
            for index in range(groups)
        ]
        texts = [_kb(APP, records[index // 2].id) for index in range(2 * groups)]
        evidence = [f"marker {index // 2:03d}" for index in range(2 * groups)]
        run, analyses, paths = _setup(
            tmp_path / str(groups), [3] * (2 * groups), texts, evidence=evidence, records=records,
        )
        known = _known(run, analyses, paths)
        assert len(known.groups) == groups
        console, _ = _render(run, analyses, paths, known)
        assert console.count("**Проблемы ") == MAX_BRIEF_ITEMS
        sizes.append(len(console))
    assert max(sizes) < 6_000
    assert abs(sizes[0] - sizes[1]) < 300


def test_legacy_render_without_known_shows_the_analysis_reference(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [2, 2], [_kb(APP), _kb(APP)])
    console, full = render_report(run, analyses, set(), "Итог.", paths)
    assert console.count("[известная проблема: payment_db_pool_1a2b3c4d]") == 2
    assert "### Известные проблемы" not in full


def test_brief_units_keep_section_order(tmp_path: Path) -> None:
    run, analyses, paths = _setup(tmp_path, [1, 5, 1], [_kb(APP), APP, _kb(APP)])
    known = _known(run, analyses, paths)

    problems = sorted(
        (_problem(e, analyses[e["file_id"]], set(), {}, {}, known) for e in run["clusters"]), key=_sort_key,
    )
    units = _brief_units("attention", problems)
    assert [[p.number for p in unit] for unit in units] == [[2], [1, 3]]


# --- сводка и история -----------------------------------------------------------------


def _hash(run: dict[str, Any], analyses: dict[str, Any], known: KnownIssues) -> str:
    return hashlib.sha256(build_summary_data(run, analyses, set(), known).encode("utf-8")).hexdigest()


def test_summary_data_names_known_issues_and_changes_after_reject(tmp_path: Path) -> None:
    record = _record()
    run, analyses, paths = _setup(tmp_path, [3, 2, 1], [_kb(APP), _kb(APP), APP], records=[record])
    known = _known(run, analyses, paths)
    data = build_summary_data(run, analyses, set(), known)
    assert "--- Известные проблемы из базы знаний (подтверждены пользователем) ---" in data
    assert "«Пул соединений БД платежей исчерпан» [приложение]: проблемы 1, 2 — 5 тестов" in data
    before = _hash(run, analyses, known)

    other = _record("unused_entry_11112222", title="Чужая запись")
    other.reject("v6:sig1")
    ProjectKB(Path(run["kb_dir"])).save(other)  # запись, на которую разборы не ссылаются
    assert _hash(run, analyses, _known(run, analyses, paths)) == before

    record.reject("v6:sig2")
    ProjectKB(Path(run["kb_dir"])).save(record)
    known = _known(run, analyses, paths)
    assert _hash(run, analyses, known) != before
    assert "Известные проблемы из базы знаний" not in build_summary_data(run, analyses, set(), known)


def test_summary_data_limits_known_issues(tmp_path: Path) -> None:
    records = [_record(f"known_{index:03d}_abcd1234", error_example=f"marker {index:03d}") for index in range(15)]
    texts = [_kb(APP, records[index // 2].id) for index in range(30)]
    evidence = [f"marker {index // 2:03d}" for index in range(30)]
    run, analyses, paths = _setup(tmp_path, [1] * 30, texts, evidence=evidence, records=records)
    data = build_summary_data(run, analyses, set(), _known(run, analyses, paths))
    assert data.count("[приложение]: проблемы") == 10
    assert "Ещё 5 известных проблем объединяют 10 проблем (10 тестов)." in data


def test_history_keeps_only_confirmed_references(tmp_path: Path) -> None:
    record = _record()
    run, analyses, paths = _setup(tmp_path, [2, 2], [_kb(APP), _kb(APP)], records=[record])
    record.reject("v6:sig2")
    ProjectKB(Path(run["kb_dir"])).save(record)
    known = _known(run, analyses, paths)
    refs = {file_id: ref.id for file_id, ref in known.refs.items()}
    records = run_records(run, analyses, set(), "run", refs)
    assert [r["kb_entry"] for r in records] == [ENTRY, None]
    assert [r["kb_entry"] for r in run_records(run, analyses, set(), "run")] == [ENTRY, ENTRY]
