"""Quality evidence must not turn unexecuted or skipped checks into a pass."""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def quality() -> ModuleType:
    script = Path(__file__).with_name("quality_harness.py")
    spec = importlib.util.spec_from_file_location("skill_quality", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _junit(tmp_path: Path, body: str) -> Path:
    xml = tmp_path / "pytest.xml"
    xml.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    return xml


def test_successful_pytest_without_expected_tests_is_inconclusive(quality, tmp_path: Path) -> None:
    results = quality.junit_results(_junit(tmp_path, ""))
    check = quality.check_result("tests/test_flow.py::test_done", results)
    assert quality.protocol_status(0, [check]) == "inconclusive"
    assert quality.protocol_status(0, []) == "inconclusive"


def test_skipped_variants_do_not_claim_full_coverage(quality, tmp_path: Path) -> None:
    results = quality.junit_results(_junit(tmp_path, """
        <testcase classname="tests.test_flow" name="test_done[normal]"/>
        <testcase classname="tests.test_flow" name="test_done[resume]">
            <skipped message="unavailable"/>
        </testcase>
    """))
    check = quality.check_result("tests/test_flow.py::test_done", results)
    assert check["status"] == "inconclusive"
    assert quality.protocol_status(0, [check]) == "inconclusive"


def test_failed_variant_wins_over_pass_and_skip(quality, tmp_path: Path) -> None:
    results = quality.junit_results(_junit(tmp_path, """
        <testcase classname="test_flow" name="test_done[normal]"/>
        <testcase classname="test_flow" name="test_done[resume]"><failure/></testcase>
        <testcase classname="test_flow" name="test_done[green]"><skipped/></testcase>
    """))
    check = quality.check_result("tests/test_flow.py::test_done", results)
    assert check["status"] == "fail"
    assert quality.protocol_status(1, [check]) == "fail"


def test_collection_error_cannot_be_a_pass(quality, tmp_path: Path) -> None:
    results = quality.junit_results(_junit(tmp_path, """
        <testcase classname="test_flow" name="test_done"/>
    """))
    check = quality.check_result("tests/test_flow.py::test_done", results)
    assert quality.protocol_status(2, [check]) == "inconclusive"
    assert quality.protocol_status(0, [check]) == "pass"


@pytest.mark.parametrize("returncode", [1, 2, -1])
def test_unrelated_failure_does_not_fail_a_completed_scenario(
    quality, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    plan = quality.make_plan(["A01", "P03"])
    output = tmp_path / "evidence"

    def execute(command, **kwargs):
        body = []
        for selected in command[5:]:
            file, name = selected.split("::")
            # The completed A01 has direct evidence; P03 failed or never completed.
            if "fix_loop" in name:
                if returncode == 1:
                    body.append(f'<testcase classname="{Path(file).stem}" name="{name}">'
                                '<failure message="proposal-independent defect"/></testcase>')
                continue
            body.append(f'<testcase classname="{Path(file).stem}" name="{name}"/>')
        _junit(output, "".join(body))
        return subprocess.CompletedProcess(command, returncode, "observed output", "")

    monkeypatch.setattr(quality.subprocess, "run", execute)
    quality.run_protocol(plan, output)
    statuses = {case["id"]: case["protocol"]["status"] for case in plan["cases"]}
    assert statuses["A01"] == "pass"
    assert statuses["P03"] == ("fail" if returncode == 1 else "inconclusive")
    assert statuses["A04"] == "not_run"
    assert plan["protocol"]["status"] == ("fail" if returncode == 1 else "inconclusive")


def test_similar_test_names_do_not_cover_expected_case(quality, tmp_path: Path) -> None:
    results = quality.junit_results(_junit(tmp_path, """
        <testcase classname="test_flow" name="test_done_eventually"/>
        <testcase classname="test_other" name="test_done"/>
    """))
    assert quality.check_result("tests/test_flow.py::test_done", results)["status"] == "inconclusive"


def test_targeted_plan_includes_negative_case_and_unscored_runtime(quality) -> None:
    plan = quality.make_plan(["P03"])
    assert [case["id"] for case in plan["cases"]] == ["P03", "A04"]
    assert all(case["agent"]["status"] == "not_run" for case in plan["cases"])
    assert all(case["agent"]["rubric"] is None for case in plan["cases"])
    assert all(case["agent"]["trace"] is None for case in plan["cases"])
    assert plan["skill"]["files"] and plan["fixtures"]["files"]
    assert all(".env" not in path for path in plan["skill"]["files"])


def test_fingerprint_detects_uncommitted_bytes_and_file_names(quality, tmp_path: Path) -> None:
    file = tmp_path / "skill.md"
    file.write_text("before", encoding="utf-8")
    before = quality.fingerprint(tmp_path, [file])["sha256"]
    file.write_text("after", encoding="utf-8")
    after = quality.fingerprint(tmp_path, [file])["sha256"]
    moved = file.rename(tmp_path / "reference.md")
    renamed = quality.fingerprint(tmp_path, [moved])["sha256"]
    assert len({before, after, renamed}) == 3


def test_protocol_run_scrubs_testops_credentials_and_keeps_agent_not_run(
    quality, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = quality.make_plan(["A01"])
    monkeypatch.setenv("ALLURE_TOKEN", "real-secret-that-must-not-be-inherited")
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://live.example")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests")
    output = tmp_path / "evidence"

    def execute(command, **kwargs):
        assert "ALLURE_TOKEN" not in kwargs["env"]
        assert "ALLURE_ENDPOINT" not in kwargs["env"]
        assert "PYTEST_ADDOPTS" not in kwargs["env"]
        assert kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        assert command[2] == "_pytest" and kwargs.get("shell") is not True
        body = []
        for selected in command[5:]:
            file, name = selected.split("::")
            body.append(f'<testcase classname="{Path(file).stem}" name="{name}"/>')
        _junit(output, "".join(body))
        return subprocess.CompletedProcess(command, 0, "local checks passed", "")

    monkeypatch.setattr(quality.subprocess, "run", execute)
    quality.run_protocol(plan, output)
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert saved["protocol"]["status"] == "pass"
    assert saved["protocol"]["counts"]["pass"] == 2
    assert all(case["agent"]["status"] == "not_run" for case in saved["cases"])
    assert "real-secret" not in (output / "report.json").read_text(encoding="utf-8")
    assert (output / "pytest.stdout.log").read_text() == "local checks passed"


def test_missing_junit_keeps_stdout_and_inconclusive_result(
    quality, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = quality.make_plan(["A01"])
    monkeypatch.setattr(quality.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, "no tests collected", ""))
    quality.run_protocol(plan, tmp_path / "evidence")
    assert plan["protocol"]["status"] == "inconclusive"
    assert plan["protocol"]["counts"] == {"pass": 0, "fail": 0, "not_run": 0}


def test_evidence_directory_is_never_overwritten(quality, tmp_path: Path) -> None:
    plan = quality.make_plan(["A01"])
    output = tmp_path / "evidence"
    output.mkdir()
    with pytest.raises(FileExistsError):
        quality.run_protocol(plan, output)


def test_copied_skill_runs_harness_without_repository_wrapper(quality, tmp_path: Path) -> None:
    installed = tmp_path / "installed-skill"
    shutil.copytree(quality.ROOT, installed, ignore=shutil.ignore_patterns(
        ".env", ".venv", "__pycache__", ".pytest_cache", "*.pyc"
    ))
    harness = installed / "tests" / "quality_harness.py"
    output = tmp_path / "standalone-evidence"
    process = subprocess.run(
        [sys.executable, str(harness), "run", "--case", "A01", "--output", str(output)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["protocol"]["counts"]["pass"] == 2
    assert report["protocol"]["status"] == "pass"
    assert all(case["agent"]["status"] == "not_run" for case in report["cases"])
    assert report["controller"]["source"] == "standalone skill"
    assert report["controller"]["pytest_config"] is None
    assert "pyproject.toml" in report["controller"]["missing"]


def test_offline_test_process_blocks_real_connections(quality, monkeypatch: pytest.MonkeyPatch) -> None:
    # Register originals for restoration after the harness sets its process-wide guard.
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)

    def execute(arguments):
        with pytest.raises(RuntimeError, match="real network disabled"):
            socket.create_connection(("live.example", 443))
        with pytest.raises(RuntimeError, match="real network disabled"):
            socket.getaddrinfo("live.example", 443)
        with socket.socket() as client:
            with pytest.raises(RuntimeError, match="real network disabled"):
                client.connect(("127.0.0.1", 443))
        return 0

    monkeypatch.setattr(pytest, "main", execute)
    assert quality.pytest_offline(["pytest.xml", "pytest.ini", "tests/test_flow.py::test_done"]) == 0
