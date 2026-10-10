"""Проверка законченного разбора для пилота: ``review``."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from skill_fake_testops import FakeTestOps
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_journal import Journal, qwen_dirs, save_main
from test_skill_flow import MARKDOWN_ANALYSIS, VALID_ANALYSIS, _finish, _prepare, _run

from alla_skill_lib import review, review_model
from alla_skill_lib import workspace as ws

LEAK = "УТЕЧКА"  # маркер данных прогона: в сводке для разработчика его быть не должно


def _report_block(out: str) -> str:
    match = re.search(r"===ОТЧЁТ===\n(.*?)\n===КОНЕЦ===", out, re.DOTALL)
    assert match, out
    return match.group(1)


def _finished_run(project: Path, capsys, monkeypatch: pytest.MonkeyPatch,
                  tmp_path: Path) -> tuple[Path, str, Path]:
    """Разбор до done из «сеанса Qwen»: (папка разбора, вывод done, папка журналов)."""
    qdir = qwen_dirs(tmp_path, project)
    monkeypatch.setenv("QWEN_CODE_SESSION_ID", "pilot-1")
    monkeypatch.setenv("QWEN_CODE_PROJECT_DIR", str(qdir))
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    done = _finish(run_dir, capsys, {order: VALID_ANALYSIS, login: MARKDOWN_ANALYSIS})
    for name in ("QWEN_CODE_SESSION_ID", "QWEN_CODE_PROJECT_DIR"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "qwen-home"))
    monkeypatch.setenv(review_model.QWEN_ENV, str(tmp_path / "нет-такого-qwen"))
    return run_dir, done, qdir


def _journal(project: Path, run_dir: Path, done: str, final: str | None = None) -> Journal:
    j = Journal("pilot-1", project)
    j.user("/alla-launch 777")
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run_dir}")
    j.call("read_file", {"file_path": str(run_dir / "clusters" / "01.md")}, "# Кластер 1")
    j.call("write_file", {"file_path": str(run_dir / "analyses" / "01.md"), "content": "…"}, "ok")
    j.skill(f"next {run_dir}", done)
    j.text(_report_block(done) if final is None else final)
    return j


# --- сквозной путь ------------------------------------------------------------------------


def test_review_of_clean_run(project: Path, testops: FakeTestOps, capsys,
                             monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    save_main(_journal(project, run_dir, done), qdir)
    state_before = (run_dir / "state.json").read_text(encoding="utf-8")

    monkeypatch.setenv("QWEN_CODE_SESSION_ID", "review-session")  # проверка из нового сеанса
    code, out = _run(["review", "--run", str(run_dir), "--no-model"], capsys)

    assert code == 0 and out.startswith("STATUS: reviewed"), out
    block = _report_block(out)
    assert "Ход разбора: чисто" in block
    assert "Качество диагнозов: не оценено" in block
    assert "Правила исполнителя: соблюдено 6 из 6" in block
    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assert facts["journal"]["found"] and facts["journal"]["reached_done"]
    assert facts["quality"]["analysed"] == 2
    assert facts["quality"]["categories"] == {"окружение": 1, "приложение": 1}
    report = (run_dir / "review" / "report.md").read_text(encoding="utf-8")
    assert "## Правила исполнителя" in report and "**нарушено**" not in report
    # проверка — не часть разбора: её сеанс в state.json не попадает
    assert (run_dir / "state.json").read_text(encoding="utf-8") == state_before


def test_review_finds_rule_violations_and_failures(
    project: Path, testops: FakeTestOps, capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    j = Journal("pilot-1", project)
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run_dir}")
    j.call("glob", {"pattern": "**/OrderTest.java"}, "Found 1 file")
    j.skill(f"verify 01 --run {run_dir}", f"STATUS: error\nНе найден файл разбора {run_dir}")
    j.skill(f"next {run_dir}", f"Traceback (most recent call last):\nKeyError: 'x' {run_dir}")
    j.call("read_file", {"file_path": str(project / "missing.txt")}, "File not found", error=True)
    j.skill(f"next {run_dir}", done)
    j.text("Отчёт готов: две проблемы.\n\nПроблемы скилла:\n1. verify не видит файл\n")
    save_main(j, qdir)

    code, out = _run(["review", "--run", str(run_dir), "--no-model"], capsys)

    assert code == 0
    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assert facts["grades"]["process"] == review.PROCESS_FAILED
    codes = {code for code, _ in facts["grades"]["process_reasons"]}
    assert {"status_error", "traceback", "rule:no_code_search", "rule:report_verbatim",
            "skill_problems", "tool_errors"} <= codes
    assert facts["journal"]["tool_errors"] == {"read_file": 1}
    summary = (run_dir / "review" / "pilot-summary.md").read_text(encoding="utf-8")
    assert "- status_error ×1" in summary and "- no_code_search: fail" in summary


def test_review_without_journal(project: Path, testops: FakeTestOps, capsys,
                                monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir, _, _ = _finished_run(project, capsys, monkeypatch, tmp_path)

    code, out = _run(["review", "--run", str(run_dir), "--no-model"], capsys)

    assert code == 0 and "журнал сеанса Qwen не найден" in out
    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assert facts["grades"]["process"] == review.PROCESS_CLEAN
    assert "Не проверены:" in (run_dir / "review" / "report.md").read_text(encoding="utf-8")


def test_review_of_unknown_run_is_error(project: Path, capsys) -> None:
    code, out = _run(["review", "--run", str(project / "alla-reports" / "nope")], capsys)
    assert code == 1 and out.startswith("STATUS: error")


# --- сводка для разработчика без данных прогона -------------------------------------------


def test_pilot_summary_has_no_run_data(project: Path, testops: FakeTestOps, capsys,
                                       monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    j = Journal("pilot-1", project)
    j.skill("prepare 777", f"STATUS: analyze\nПапка разбора: {run_dir}")
    j.skill(f"verify 01 --run {run_dir}", f"STATUS: error\n{LEAK}-ошибка {run_dir}")
    j.call("read_file", {"file_path": f"/{LEAK}/x"}, f"{LEAK}", error=True)
    j.skill(f"next {run_dir}", done)
    j.text(f"{_report_block(done)}\n\nПроблемы скилла:\n1. {LEAK}-проблема\n")
    for record in j.records:
        if record["type"] == "assistant":
            record["model"] = f"evil {LEAK} model"
    save_main(j, qdir)
    run = ws.read_json(run_dir / "run.json")
    facts = review.collect_facts(ws.RunPaths(run_dir))
    facts["assessment"] = {
        "problems": [{"number": 1, "cause": "частично", "category": "не согласен",
                      "category_expected": "тест", "actions": "общие", "note": f"{LEAK}-замечание"}],
        "summary": f"{LEAK}-итог", "tests_covered": 2, "usage": {"total": 10}, "seconds": 3,
    }
    facts["grades"] = review.grades(facts)

    summary = review.render_pilot_summary(facts)

    forbidden = [LEAK, str(project), run_dir.name, "Regression", "OrderTest", "LoginTest",
                 "customer is null", "auth-service", "Connection refused", "777"]
    forbidden += [entry["label"] for entry in run["clusters"] if entry.get("label")]
    forbidden += [str(item.get("cause")) for item in facts["clusters"] if item.get("cause")]
    for text in forbidden:
        assert text not in summary, text
    assert "модель: ?" in summary and "причина: частично ×1" in summary


# --- оценки -------------------------------------------------------------------------------


@pytest.mark.parametrize(("causes", "disagree", "grade"), [
    (["подтверждена"] * 9 + ["частично"], 1, review.QUALITY_GOOD),
    (["подтверждена"] * 9 + ["частично"], 2, review.QUALITY_FAIR),
    (["подтверждена"] * 5 + ["не подтверждена"] * 5, 0, review.QUALITY_FAIR),
    (["подтверждена"] * 4 + ["не подтверждена"] * 6, 0, review.QUALITY_POOR),
])
def test_quality_grade_thresholds(causes: list[str], disagree: int, grade: str) -> None:
    problems = [{"cause": cause, "category": "не согласен" if i < disagree else "согласен"}
                for i, cause in enumerate(causes)]
    assert review.quality_grade({"problems": problems})[0] == grade


def test_partial_assessment_is_never_good() -> None:
    """Модель вернула одну оценку из десяти: пропущенные — не подтверждения."""
    one = {"problems": [{"cause": "подтверждена", "category": "согласен"}], "requested": 10}
    grade, note = review.quality_grade(one)
    assert grade == review.QUALITY_POOR and "оценка частичная" in note
    nine = {"problems": [{"cause": "подтверждена", "category": "согласен"}] * 9, "requested": 10}
    assert review.quality_grade(nine)[0] == review.QUALITY_FAIR


def test_quality_without_assessment() -> None:
    assert review.quality_grade(None) == (review.QUALITY_NONE, "оценка модели не получена")
    assert review.quality_grade({"error": "qwen не найден"}) == (review.QUALITY_NONE, "qwen не найден")


# --- проверяющая модель -------------------------------------------------------------------


FAKE_QWEN = """#!{python}
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
log = {{"argv": argv, "env": sorted(k for k in os.environ if k.startswith("QWEN_CODE_")),
       "cwd": os.getcwd(), "prompt": prompt}}
open({log!r}, "w", encoding="utf-8").write(json.dumps(log, ensure_ascii=False))
mode = {mode!r}
if mode == "fail":
    print("wall-clock budget exceeded", file=sys.stderr); sys.exit(55)
if mode == "garbage":
    print("не json"); sys.exit(0)
answer = {{"problems": [
    {{"number": 1, "cause": "подтверждена", "category": "согласен", "category_expected": "",
     "actions": "полезны", "note": "NPE есть в логе"}},
    {{"number": 3, "cause": "частично", "category": "не согласен", "category_expected": "окружение",
     "actions": "общие", "note": "лог сервиса не приложен"}},
    {{"number": 99, "cause": "подтверждена", "category": "согласен", "category_expected": "",
     "actions": "полезны", "note": "лишняя"}}],
    "summary": "Разборам можно доверять."}}
events = [{{"type": "system", "subtype": "init"}},
          {{"type": "result", "subtype": "success", "usage": {{"input_tokens": 900, "output_tokens": 100}},
           "structured_output": answer, "result": ""}}]
print(json.dumps(events, ensure_ascii=False))
"""


def _fake_qwen(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    log = tmp_path / f"qwen-{mode}.json"
    script = tmp_path / f"qwen-{mode}"
    script.write_text(FAKE_QWEN.format(python=sys.executable, log=str(log), mode=mode), encoding="utf-8")
    script.chmod(0o755)
    return script, log


def test_model_assessment_with_clean_context(project: Path, testops: FakeTestOps, capsys,
                                             monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir, done, qdir = _finished_run(project, capsys, monkeypatch, tmp_path)
    save_main(_journal(project, run_dir, done), qdir)
    script, log = _fake_qwen(tmp_path, "ok")
    monkeypatch.setenv(review_model.QWEN_ENV, str(script))
    monkeypatch.setenv("QWEN_CODE_SESSION_ID", "parent-session")

    code, out = _run(["review", "--run", str(run_dir)], capsys)

    assert code == 0, out
    call = json.loads(log.read_text(encoding="utf-8"))
    assert call["env"] == []  # сеанс родителя проверяющему не передаётся
    assert not Path(call["cwd"]).is_relative_to(project)  # без контекста проекта
    assert "--json-schema" in call["argv"] and "plan" in call["argv"]
    assert "=== ПРОБЛЕМА 1" in call["prompt"] and "----- РАЗБОР АГЕНТА -----" in call["prompt"]
    assert "customer is null" in call["prompt"]  # разбор и данные задания
    assert "public class OrderTest" in call["prompt"]  # код из «Где искать код автотеста»
    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assessment = facts["assessment"]
    # модель разбирала проблемы 1 и 3 (2 — скрипт); 99 не запрашивалась
    assert [p["number"] for p in assessment["problems"]] == [1, 3]
    assert assessment["usage"] == {"input": 900, "output": 100, "total": 1000}
    assert facts["grades"]["quality"] == review.QUALITY_FAIR
    report = (run_dir / "review" / "report.md").read_text(encoding="utf-8")
    assert "скорее «окружение»" in report and "Разборам можно доверять." in report


@pytest.mark.parametrize(("mode", "error_code"), [("fail", "timeout"), ("garbage", "bad_output")])
def test_model_failures_leave_report_without_assessment(
    mode: str, error_code: str, project: Path, testops: FakeTestOps, capsys,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    run_dir, _, _ = _finished_run(project, capsys, monkeypatch, tmp_path)
    script, _ = _fake_qwen(tmp_path, mode)
    monkeypatch.setenv(review_model.QWEN_ENV, str(script))

    code, out = _run(["review", "--run", str(run_dir)], capsys)

    assert code == 0 and "Качество диагнозов: не оценено" in out
    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assert facts["assessment"]["error_code"] == error_code
    summary = (run_dir / "review" / "pilot-summary.md").read_text(encoding="utf-8")
    assert f"- нет ({error_code})" in summary


def test_missing_qwen_is_reported(project: Path, testops: FakeTestOps, capsys,
                                  monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir, _, _ = _finished_run(project, capsys, monkeypatch, tmp_path)
    monkeypatch.delenv(review_model.QWEN_ENV)
    monkeypatch.setattr(review_model.shutil, "which", lambda name: None)

    _run(["review", "--run", str(run_dir)], capsys)

    facts = json.loads((run_dir / "review" / "facts.json").read_text(encoding="utf-8"))
    assert facts["assessment"]["error_code"] == "qwen_not_found"


def test_categories_come_from_analysis_reference() -> None:
    rows = review_model.categories_table().splitlines()
    assert [row.split("`")[1] for row in rows] == list(review_model.CATEGORIES)


def test_largest_problems_are_reviewed_first() -> None:
    clusters = [{"number": n, "tests": t, "auto": a, "analysed": True}
                for n, t, a in [(1, 2, False), (2, 9, False), (3, 9, False), (4, 50, True)]]
    clusters += [{"number": 10 + n, "tests": 1, "auto": False, "analysed": True} for n in range(12)]
    selected = review_model.select_problems({"clusters": clusters})
    assert [item["number"] for item in selected[:3]] == [2, 3, 1]
    assert len(selected) == review_model.MAX_REVIEWED and all(not item["auto"] for item in selected)


def test_parse_output_reads_tool_use_and_text_result() -> None:
    answer = {"problems": [], "summary": "ок"}
    tool_use = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "structured_output", "input": answer}]}},
        {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2}}]
    assert review_model.parse_output(json.dumps(tool_use)) == (answer, {"input": 1, "output": 2, "total": 3})
    text = {"type": "result", "result": "Ответ: " + json.dumps(answer, ensure_ascii=False)}
    assert review_model.parse_output(json.dumps(text))[0] == answer
    assert review_model.parse_output("мусор") == (None, {})


def test_long_note_is_cut_with_ellipsis() -> None:
    cell = review._cell("слово " * 100)
    assert len(cell) == 300 and cell.endswith("…") and "|" not in cell.replace("\\|", "")
