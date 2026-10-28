"""Новые общие контракты проверяются отдельно от механики тикетов."""
from __future__ import annotations

import json
import copy
import os
import shutil
import subprocess
import sys
import uuid
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
        'import json,os\nfrom pathlib import Path\n'
        'c=json.loads(Path(os.environ["ALLA_CONTEXT"]).read_text())\n'
        'assert "run_dir" not in c\nprint("STATUS: ok")\n'
        'print(c["request_id"])\n'))
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    original, _, _ = _prepare(project, capsys)
    reports = project / "custom-reports"
    reports.mkdir()
    destination = reports / original.name
    shutil.move(str(original), destination)
    paths = ws.RunPaths(destination)
    request_id = str(uuid.uuid4())
    directory = data.state_dir(reports, "demo", project)
    ws.write_json(directory / "requests" / f"{request_id}.json", {
        "schema": 1, "extension": "demo", "project_root": str(project.resolve()),
        "request_id": request_id, "run_uid": ws.run_uid(paths),
        "state": "created", "receipt": {"id": "X-1", "url": "https://tracker/X-1"}})
    code, output = _run(["clean", "--reports-dir", str(reports), "--older-than-days", "0"], capsys)
    assert code == 0, output
    assert not destination.exists() and (directory / "requests").is_dir()
    code, output = ext_cli(project, capsys, "reconcile", "demo", "--reports-dir", str(reports),
                            "--request-id", request_id)
    assert code == 0 and request_id in output, output
    assert len(list((directory / "contexts").glob("*.json"))) == 1


def test_snapshot_feedback_requires_successful_remember_and_tracks_reject(project: Path,
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
    assert current["category"] == "окружение"
    assert current["cause"] == "Пул стенда исчерпан."
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


def test_snapshot_export_refuses_foreign_project(project: Path,
        testops: FakeTestOps, capsys, tmp_path: Path) -> None:
    make_demo(project, extension_toml=MANIFEST.replace(
        'event = "after_apply"', 'event = "on_request_many"'))
    run_dir, _, _ = _prepare(project, capsys)
    assert ext_cli(project, capsys, "enable", "demo")[0] == 0
    assert ext_cli(project, capsys, "run", "demo", "1", "--run", str(run_dir))[0] == 0
    context_file = next((run_dir / "extensions" / "demo").glob("on_request_many-*.json"))
    code, output = _run(["extensions", "snapshot", "demo", "--context", str(context_file),
                         "--project-root", str(tmp_path / "foreign"),
                         "--reports-dir", str(run_dir.parent)], capsys)
    assert code != 0 and "не принадлежит" in output
