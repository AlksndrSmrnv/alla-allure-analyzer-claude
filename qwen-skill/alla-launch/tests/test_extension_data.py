"""Новые общие контракты проверяются отдельно от механики тикетов."""
from __future__ import annotations

import json
import copy
import os
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import FakeTestOps
from test_skill_extensions import MANIFEST, ext_cli, make_demo
from test_skill_flow import VALID_ANALYSIS, _prepare, _run
from test_skill_related import CUSTOMER, CUSTOMER_STEP, NPE, _next, _observation, _step_split_run

from alla_skill_lib import extension_data as data
from alla_skill_lib import extensions as ext
from alla_skill_lib import workspace as ws
from alla_skill_lib.extensions import ExtensionError, load_manifest


def test_uid_publish_once_in_parallel_processes_and_reused_folder(tmp_path: Path) -> None:
    root = tmp_path / "same"
    root.mkdir()
    script = ("from pathlib import Path; from alla_skill_lib.workspace import RunPaths,run_uid;"
              "import sys; print(run_uid(RunPaths(Path(sys.argv[1]))))")
    env = {**os.environ, "PYTHONPATH": str(Path(ws.__file__).parent.parent)}
    def read(_: int) -> str:
        return subprocess.check_output([sys.executable, "-c", script, str(root)],
                                       env=env, text=True).strip()
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(read, range(16)))
    assert len(set(values)) == 1
    assert ws.run_uid(ws.RunPaths(root)) == values[0]
    shutil.rmtree(root)
    root.mkdir()
    assert ws.run_uid(ws.RunPaths(root)) != values[0]


def test_uid_same_timestamp_after_clean_and_corruption(tmp_path: Path) -> None:
    now = datetime(2026, 10, 28, 10, 0)
    first = ws.create_run_dir(tmp_path, 777, now)
    uid = ws.run_uid(first)
    shutil.rmtree(first.root)
    second = ws.create_run_dir(tmp_path, 777, now)
    assert second.root == first.root
    assert ws.run_uid(second) != uid
    (second.root / "run.uid").write_text("broken")
    with pytest.raises(ws.RunNotFoundError, match="Идентификатор"):
        ws.run_uid(second)
    assert (second.root / "run.uid").read_text() == "broken"


def test_request_many_is_one_unique_context_and_exact_text(project: Path,
        testops: FakeTestOps, capsys) -> None:
    folder = make_demo(project, extension_toml=MANIFEST.replace(
        'event = "after_apply"', 'event = "on_request_many"'))
    run_dir, _, _ = _prepare(project, capsys)
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    directory = run_dir / "extensions" / "demo"
    directory.mkdir(parents=True)
    text = "  вопрос\r\n" + "д" * 15_000 + "\nпроверка\t \n"
    source = directory / "question.txt"
    source.write_bytes(text.encode("utf-8"))
    for _ in range(2):
        code, output = ext_cli(project, capsys, "run", "demo", "2", "1", "2", "999",
                               "--run", str(run_dir), "--text-file", "question_1", str(source))
        assert code == 0, output
        assert output.count("prompt дословно") == 1
    contexts = sorted(directory.glob("on_request_many-*.json"))
    assert len(contexts) == 2
    content = json.loads(contexts[0].read_text())
    assert content["selected_numbers"] == [1, 2, 999]
    assert content["text_params"]["question_1"] == text
    assert Path(content["state_dir"]).is_relative_to(run_dir.parent)
    assert content["run_uid"] == ws.run_uid(ws.RunPaths(run_dir))
    assert load_manifest(folder, bundled=False).hooks_for("on_request_many")


def test_conflicting_request_events_rejected(project: Path) -> None:
    folder = make_demo(project, extension_toml=MANIFEST.replace(
        'event = "after_apply"', 'event = "on_request_many"').replace(
        'event = "after_revert"', 'event = "on_request"'))
    with pytest.raises(ExtensionError, match="одновременно"):
        load_manifest(folder, bundled=False)


def test_text_file_rejects_overflow_and_outside_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "out"
    directory.mkdir()
    source = directory / "q.txt"
    source.write_text("я" * 16_001)
    with pytest.raises(ExtensionError, match="16000"):
        data.text_files([["question_2", str(source)]], directory)
    external = tmp_path / "secret.txt"
    external.write_text("outside")
    source.unlink()
    source.symlink_to(external)
    with pytest.raises(ExtensionError):
        data.text_files([["question_2", str(source)]], directory)
    with pytest.raises(ExtensionError):
        data.text_files([["question_2", str(external)]], directory)


def test_snapshot_prefers_current_confirmed_and_sees_invalid_sources(project: Path,
        testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    paths = ws.RunPaths(run_dir)
    entry = next(e for e in run["clusters"] if not e["auto"])
    file_id = entry["file_id"]
    paths.analysis(file_id).write_text(VALID_ANALYSIS + "\nСОГЛАСОВАННОСТЬ: одна причина\n")
    paths.confirmed(file_id).parent.mkdir()
    ws.write_json(paths.confirmed(file_id), {"category": "тест", "cause": "Проверено инженером",
                                            "fix": ["Исправить проверку"], "details": []})
    first = data.snapshot(paths, {"example": True})
    problem = next(p for p in first["problems"] if p["number"] == int(file_id))
    assert problem["category"] == "тест" and problem["cause"] == "Проверено инженером"
    assert problem["confirmed"] and problem["sources"] and problem["tests"]
    assert problem["valid"]
    paths.sources(file_id).unlink()
    problem = next(p for p in data.snapshot(paths, {})["problems"]
                   if p["number"] == int(file_id))
    assert not problem["valid"]


def test_reconcile_after_clean_uses_actual_reports_without_context(project: Path,
        testops: FakeTestOps, capsys) -> None:
    make_demo(project, extension_toml=MANIFEST.replace('act = "act.py"',
              'act = "act.py"\nreconcile = "reconcile.py"'), reconcile_py=(
        'import json,os,sys\nfrom pathlib import Path\n'
        'c=json.loads(Path(os.environ["ALLA_CONTEXT"]).read_text())\n'
        'assert "run_dir" not in c and "request" not in c\n'
        'rid=sys.argv[sys.argv.index("--request")+1]\n'
        'r=json.loads((Path(c["state_dir"])/"custom-log.json").read_text())\n'
        'assert r["key"]==rid\nprint("STATUS: ok")\nprint(rid)\n'))
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    original, _, _ = _prepare(project, capsys)
    reports = project / "custom-reports"
    reports.mkdir()
    destination = reports / original.name
    shutil.move(str(original), destination)
    request_id = str(uuid.uuid4())
    directory = data.state_dir(reports, "demo", project)
    ws.write_json(directory / "custom-log.json", {"key": request_id})
    code, output = _run(["clean", "--reports-dir", str(reports), "--older-than-days", "0"], capsys)
    assert code == 0, output
    assert not destination.exists() and (directory / "custom-log.json").is_file()
    code, output = ext_cli(project, capsys, "reconcile", "demo", "--reports-dir", str(reports),
                            "--request-id", request_id)
    assert code == 0 and request_id in output, output
    assert not list((directory / "contexts").glob("*.json"))
    code, output = ext_cli(project, capsys, "action", "demo", "reconcile", "--reports-dir",
                           str(reports), "--", "--request", request_id)
    assert code == 0 and request_id in output, output
    assert not list((directory / "contexts").glob("*.json"))


def test_snapshot_accepts_feedback_only_after_successful_remember(project: Path,
        testops: FakeTestOps, capsys) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    paths = ws.RunPaths(run_dir)
    entry = next(e for e in run["clusters"] if not e["auto"])
    file_id = entry["file_id"]
    paths.analysis(file_id).write_text(VALID_ANALYSIS + "\nСОГЛАСОВАННОСТЬ: одна причина\n")
    paths.feedback(file_id).write_text("ПРИЧИНА: окружение — Пул стенда исчерпан.\n"
                                      "КАК ИСПРАВИТЬ:\n1. Восстановить пул стенда.\n")
    def problem() -> dict:
        return next(p for p in data.snapshot(paths, {})["problems"]
                    if p["number"] == int(file_id))
    assert not problem()["valid"]
    code, output = _run(["remember", file_id, "--run", str(run_dir)], capsys)
    assert code == 0, output
    current = problem()
    assert current["valid"] and current["confirmed"]
    assert not current["feedback_unconfirmed"]
    assert current["category"] == "окружение"
    assert current["cause"] == "Пул стенда исчерпан."
    with paths.feedback(file_id).open("a", encoding="utf-8") as handle:
        handle.write("2. Перезапустить сервис.\n")
    edited = problem()
    assert not edited["valid"] and not edited["confirmed"] and edited["feedback_unconfirmed"]
    assert _run(["remember", file_id, "--entry", json.loads(next(
        Path(entry["kb_dir"]).glob("*.json")).read_text())["id"], "--run", str(run_dir)],
        capsys)[0] == 0
    assert problem()["confirmed"]
    record_path = next(Path(entry["kb_dir"]).glob("*.json"))
    record = json.loads(record_path.read_text())
    code, output = _run(["reject", file_id, record["id"], "--run", str(run_dir)], capsys)
    assert code == 0, output
    assert not problem()["valid"]


def test_related_scope_survives_other_candidate_group_changes(project: Path,
        monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run_dir, ids = _step_split_run(project, monkeypatch, capsys)
    paths = ws.RunPaths(run_dir)
    assert _next(run_dir, capsys).startswith("STATUS: relate")
    paths.related.write_text("ОДНА ПРИЧИНА: 1, 2 — покупатель не заполнен\n")
    assert _next(run_dir, capsys).startswith("STATUS: summary")
    assert data.snapshot(paths, {})["groups"][0]["numbers"] == [1, 2]
    paths.related.write_text("РАЗНЫЕ: 1, 2 — разные причины\n")
    assert data.snapshot(paths, {})["groups"] == []
    paths.related.write_text("ОДНА ПРИЧИНА: 1, 2 — покупатель не заполнен\n")
    run = ws.read_run(paths)
    for number in (3, 4):
        entry = copy.deepcopy(run["clusters"][0])
        file_id = f"{number:02d}"
        entry.update(file_id=file_id, cluster_id=f"independent-{number}")
        run["clusters"].append(entry)
        shutil.copyfile(paths.sources(ids[0]), paths.sources(file_id))
        text = NPE.format(cause="на общем стенде не запущен сервис авторизации пользователей",
                          step="Запустить сервис авторизации пользователей на стенде",
                          observation=_observation(run_dir, file_id))
        paths.analysis(file_id).write_text(text.replace("приложение —", "окружение —"))
    ws.write_json(paths.run_json, run)
    assert any(group["numbers"] == [1, 2] for group in data.snapshot(paths, {})["groups"])
    paths.analysis(ids[0]).write_text(NPE.format(cause=CUSTOMER + " новая причина",
        step=CUSTOMER_STEP, observation=_observation(run_dir, ids[0])))
    assert not any(group["numbers"] == [1, 2] for group in data.snapshot(paths, {})["groups"])


def test_action_refuses_foreign_project(project: Path,
        testops: FakeTestOps, capsys, tmp_path: Path) -> None:
    make_demo(project, extension_toml=MANIFEST.replace(
        'event = "after_apply"', 'event = "on_request_many"'))
    run_dir, _, _ = _prepare(project, capsys)
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    assert ext_cli(project, capsys, "run", "demo", "1", "--run", str(run_dir))[0] == 0
    context_file = next((run_dir / "extensions" / "demo").glob("on_request_many-*.json"))
    code, output = _run(["extensions", "action", "demo", "act", "--context", str(context_file),
                         "--project-root", str(tmp_path / "foreign"),
                         "--reports-dir", str(run_dir.parent)], capsys)
    assert code != 0


def test_single_snapshot_channel_refreshes_before_every_action(project: Path,
        testops: FakeTestOps, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    make_demo(project, extension_toml=MANIFEST.replace('api = 1', 'api = 1\nsnapshot = true').replace(
        'event = "after_apply"', 'event = "on_request_many"'), act_py=(
        'import json,os\nfrom pathlib import Path\n'
        'c=json.loads(Path(os.environ["ALLA_CONTEXT"]).read_text())\n'
        'assert "snapshot_command" not in c\n'
        's=json.loads(Path(c["snapshot_path"]).read_text())\n'
        'assert s["settings"] == c["settings"]\n'
        'print("STATUS: ok")\nprint(s["problems"][0]["cause"])\n'))
    run_dir, run, _ = _prepare(project, capsys)
    paths = ws.RunPaths(run_dir)
    first = run["clusters"][0]["file_id"]
    paths.analysis(first).write_text(VALID_ANALYSIS + "\nСОГЛАСОВАННОСТЬ: одна причина\n")
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    original = data.snapshot
    seen = []
    def capture(paths, settings):
        seen.append(paths)
        return original(paths, settings)
    monkeypatch.setattr(data, "snapshot", capture)
    assert ext_cli(project, capsys, "run", "demo", "1", "--run", str(run_dir))[0] == 0
    context_file = next(paths.extension_dir("demo").glob("on_request_many-*.json"))
    assert len(seen) == 1
    original_files = set(paths.extension_dir("demo").glob("*.json"))
    for cause in ("Первый проверенный диагноз", "Второй проверенный диагноз"):
        ws.write_json(paths.confirmed(first), {"category": "приложение", "cause": cause,
                                              "fix": ["Исправить"], "details": []})
        code, output = ext_cli(project, capsys, "action", "demo", "act",
                               "--context", str(context_file))
        assert code == 0 and cause in output, output
        assert set(paths.extension_dir("demo").glob("*.json")) == original_files
    assert len(seen) == 3


@pytest.mark.parametrize("status", ["error", "exception"])
def test_action_context_and_snapshot_removed_on_failure(project: Path,
        testops: FakeTestOps, capsys, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    make_demo(project, extension_toml=MANIFEST.replace('api = 1', 'api = 1\nsnapshot = true').replace(
        'event = "after_apply"', 'event = "on_request_many"'))
    run_dir, _, _ = _prepare(project, capsys)
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    assert ext_cli(project, capsys, "run", "demo", "1", "--run", str(run_dir))[0] == 0
    directory = run_dir / "extensions" / "demo"
    source = next(directory.glob("on_request_many-*.json"))
    original_files = set(directory.glob("*.json"))
    executed = []
    def execute(extension, script, context, project, args, *, timeout):
        content = ws.read_json(context)
        assert context.is_file() and Path(content["snapshot_path"]).is_file()
        executed.append(context)
        if status == "exception":
            raise OSError("synthetic execution failure")
        return 1, "STATUS: error\nsynthetic script error"
    monkeypatch.setattr(ext, "run_script", execute)
    if status == "exception":
        with pytest.raises(OSError, match="synthetic"):
            ext.run_action(project, run_dir.parent, "demo", "act", str(source), [])
    else:
        result, _ = ext.run_action(project, run_dir.parent, "demo", "act", str(source), [])
        assert result == "error"
    assert len(executed) == 1
    assert set(directory.glob("*.json")) == original_files


def test_snapshot_work_consumes_action_budget(project: Path, testops: FakeTestOps,
        capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    make_demo(project, extension_toml=MANIFEST.replace('timeout = 5', 'timeout = 110').replace(
        'api = 1', 'api = 1\nsnapshot = true').replace(
        'event = "after_apply"', 'event = "on_request_many"'))
    run_dir, _, _ = _prepare(project, capsys)
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    assert ext_cli(project, capsys, "run", "demo", "1", "--run", str(run_dir))[0] == 0
    context_file = next((run_dir / "extensions" / "demo").glob("on_request_many-*.json"))
    elapsed = [100.0]
    monkeypatch.setattr(ext, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    def build(context, paths, destination=None):
        elapsed[0] += 30
    def execute(extension, script, path, project, args, *, timeout):
        context = ws.read_json(path)
        assert context["budget_s"] == 75.0
        assert "deadline_monotonic" not in context
        assert timeout == ext.ACTION_LIMIT_S - 30
        return 0, "STATUS: ok\nготово"
    monkeypatch.setattr(data, "refresh_snapshot", build)
    monkeypatch.setattr(ext, "run_script", execute)
    assert ext.run_action(project, run_dir.parent, "demo", "act", str(context_file), []) == (
        "ok", "готово")


def test_tickets_generic_reconcile_after_clean_with_lost_response(project: Path,
        testops: FakeTestOps, capsys) -> None:
    bundled = Path(__file__).parents[1] / "extensions" / "tickets"
    shutil.copytree(bundled, ws.SKILL_DIR / "extensions" / "tickets")
    run_dir, run, _ = _prepare(project, capsys)
    paths = ws.RunPaths(run_dir)
    first = next(entry["file_id"] for entry in run["clusters"] if not entry["auto"])
    paths.analysis(first).write_text(VALID_ANALYSIS + "\nСОГЛАСОВАННОСТЬ: одна причина\n")
    code, output = ext_cli(project, capsys, "enable", "tickets")
    assert code == 0, output
    program = project / ".venv" / "bin" / "python"
    program.parent.mkdir(parents=True, exist_ok=True)
    if not program.exists():
        program.symlink_to(sys.executable)
    demo = Path(__file__).parents[1] / "extensions" / "tickets" / "adapters" / "demo.py"
    command = [".venv/bin/python", str(demo), "--db", str(project / "tracker.sqlite"),
               "--lose-response-once"]
    with ext.config_path(project).open("a") as handle:
        handle.write('\n[tickets]\nteam_application = "backend"\nadapter_command = '
                     + json.dumps(command) + '\n')
    assert ext_cli(project, capsys, "run", "tickets", str(int(first)), "--run", str(run_dir))[0] == 0
    context_file = next(paths.extension_dir("tickets").glob("on_request_many-*.json"))
    def action(name, *args):
        code, output = ext_cli(project, capsys, "action", "tickets", name,
                               "--context", str(context_file), "--", *args)
        assert code == 0, output
        return output
    action("draft", "--unit", str(int(first)))
    directory = data.state_dir(run_dir.parent, "tickets", project)
    record_path = next((directory / "requests").glob("*.json"))
    record = ws.read_json(record_path)
    action("show", "--request", record["request_id"])
    action("submit", "--request", record["request_id"], "--shown", record["version_hash"])
    assert ws.read_json(record_path)["state"] == "unknown"
    assert _run(["clean", "--reports-dir", str(run_dir.parent),
                 "--older-than-days", "0"], capsys)[0] == 0
    assert not run_dir.exists()
    code, output = ext_cli(project, capsys, "action", "tickets", "reconcile", "--reports-dir",
                           str(run_dir.parent), "--", "--request", record["request_id"])
    assert code == 0 and "DEMO-1" in output, output
    assert ws.read_json(record_path)["state"] == "created"
