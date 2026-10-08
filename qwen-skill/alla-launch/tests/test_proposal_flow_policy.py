"""New edit restrictions preserve recorded applications, but reject unapplied proposals."""

import hashlib
from pathlib import Path

import pytest
from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import FakeTestOps
from test_skill_flow import MARKDOWN_ANALYSIS, PROPOSAL, TEST_ANALYSIS, _finish, _next, _prepare, _run

from alla_skill_lib import cli, workspace as ws
from alla_skill_lib.proposals import Proposal, _proposal_hash, parse_proposal


@pytest.mark.parametrize("contents", ["a();\n", "b();\n"])
def test_next_rejects_unrecorded_config_even_when_after_is_already_present(
    tmp_path: Path, contents: str,
) -> None:
    (tmp_path / "playwright.config.ts").write_text(contents, encoding="utf-8")
    paths = ws.RunPaths(tmp_path / "alla-reports" / "old")
    paths.proposal("01").parent.mkdir(parents=True)
    text = (
        "РЕШЕНИЕ: исправить\nФАЙЛ: playwright.config.ts:1\n"
        "БЫЛО:\na();\nСТАЛО:\nb();\nПОЧЕМУ: нужен другой вызов\n"
    )
    ws.write_text(paths.proposal("01"), text)
    entry = {"file_id": "01", "label": "Тест", "member_count": 1}
    outcome = cli._proposal_step(paths, {"attempts": {}}, entry, 1, 1, tmp_path)
    assert isinstance(outcome, tuple) and outcome[0] == "fix"
    assert "РЕШЕНИЕ: не трогать" in outcome[1] and "ПОЧЕМУ" in outcome[1]

    ws.write_text(paths.proposal("01"), "РЕШЕНИЕ: не трогать\nПОЧЕМУ: изменить конфиг вручную\n")
    accepted = cli._proposal_step(paths, {}, entry, 1, 1, tmp_path)
    assert isinstance(accepted, Proposal) and not accepted.is_fix


@pytest.mark.parametrize("contents", ["b();\n", "c();\n"])
@pytest.mark.parametrize("matching_record", [True, False])
def test_next_accepts_now_denied_application_only_with_matching_record(
    tmp_path: Path, contents: str, matching_record: bool,
) -> None:
    (tmp_path / "build.gradle.kts").write_text(contents, encoding="utf-8")
    paths = ws.RunPaths(tmp_path / "alla-reports" / "old")
    paths.proposal("01").parent.mkdir(parents=True)
    text = (
        "РЕШЕНИЕ: исправить\nФАЙЛ: build.gradle.kts:1\n"
        "БЫЛО:\na();\nСТАЛО:\nb();\nПОЧЕМУ: нужен другой вызов\n"
    )
    proposal = parse_proposal(text)
    ws.write_text(paths.proposal("01"), text)
    ws.write_atomic_bytes(paths.proposal_backup("01"), b"a();\n")
    ws.write_json(paths.proposal_record("01"), {
        "proposal": _proposal_hash(proposal) if matching_record else "different",
        "file": "build.gradle.kts", "line": 1,
        "sha_before": hashlib.sha256(b"a();\n").hexdigest(),
        "sha_after": hashlib.sha256(b"b();\n").hexdigest(),
        "backup": paths.proposal_backup("01").name,
    })
    entry = {"file_id": "01", "label": "Тест", "member_count": 1}
    outcome = cli._proposal_step(paths, {"attempts": {}}, entry, 1, 1, tmp_path)
    if matching_record:
        assert isinstance(outcome, Proposal)
    else:
        assert isinstance(outcome, tuple) and outcome[0] == "fix"


def test_done_does_not_offer_apply_again_for_an_ordinary_applied_file(
    project: Path, testops: FakeTestOps, capsys,
) -> None:
    run_dir, run, _ = _prepare(project, capsys)
    order, login = [entry["file_id"] for entry in run["clusters"] if not entry["auto"]]
    (run_dir / "analyses" / f"{order}.md").write_text(TEST_ANALYSIS, encoding="utf-8")
    assert _next(run_dir, capsys).startswith("STATUS: propose")
    (run_dir / "proposals" / f"{order}.md").write_text(PROPOSAL, encoding="utf-8")
    assert f"apply {order} --run" in _finish(run_dir, capsys, {login: MARKDOWN_ANALYSIS})
    code, diff = _run(["apply", str(int(order)), "--run", str(run_dir)], capsys)
    assert code == 0
    confirm = next(line for line in diff.splitlines() if "--yes --diff" in line)
    digest = confirm.split("--diff ")[1].split()[0]
    code, applied = _run(["apply", str(int(order)), "--run", str(run_dir),
                          "--yes", "--diff", digest], capsys)
    assert code == 0 and applied.startswith("STATUS: applied")
    done = _next(run_dir, capsys)
    assert "— уже применено" in done
    assert f"apply {order} --run" not in done
