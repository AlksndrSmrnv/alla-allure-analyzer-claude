"""Edit policy rejects build/config targets without losing earlier apply records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from skill_fixtures import without_libmagic  # noqa: F401

from alla_skill_lib.code_hints import SKIP_DIRS
from alla_skill_lib.proposals import (
    Proposal,
    ProposalFiles,
    _proposal_hash,
    apply_proposal,
    applied_state,
    parse_proposal,
    revert_proposal,
    validate_proposal,
)


def _proposal(path: str) -> Proposal:
    return parse_proposal(
        f"РЕШЕНИЕ: исправить\nФАЙЛ: {path}:1\nБЫЛО:\na();\n"
        "СТАЛО:\nb();\nПОЧЕМУ: исправить ошибочный вызов\n"
    )


def _previous_apply(root: Path, path: str) -> tuple[Proposal, ProposalFiles, Path, bytes]:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    original, updated = b"a();\n", b"b();\n"
    target.write_bytes(updated)
    proposal = _proposal(path)
    folder = root / "alla-reports" / "old" / "proposals"
    folder.mkdir(parents=True)
    files = ProposalFiles(folder / "01.applied.json", folder / "01.orig", folder / "01.patch")
    files.backup.write_bytes(original)
    files.record.write_text(json.dumps({
        "proposal": _proposal_hash(proposal), "file": path, "line": 1,
        "sha_before": hashlib.sha256(original).hexdigest(),
        "sha_after": hashlib.sha256(updated).hexdigest(), "backup": files.backup.name,
    }), encoding="utf-8")
    return proposal, files, target, original


@pytest.mark.parametrize(
    "path",
    [
        "build/T.java", "module/target/Test.kt", "out/test.py", "dist/test.js",
        "venv/test.py", "buildSrc/src/main/Plugin.kt", "build-logic/src/main/Plugin.java",
        "alla-kb/recipe.py", "build.gradle.kts", "module/settings.gradle.kts",
        "gradle/quality.gradle.kts", "setup.py", "noxfile.py", "conanfile.py",
        "Jenkinsfile.groovy", "gulpfile.js", "gulpfile.mjs", "gulpfile.ts", "Gruntfile.js",
        "playwright.config.ts", "playwright.config.ci.ts", "cypress.config.js",
        "jest.config.mjs", "vitest.config.ts", "vite.config.ts", "webpack.config.js",
        "rollup.config.js", "babel.config.js", "eslint.config.js", "postcss.config.js",
        "tailwind.config.ts", "next.config.mjs", "nuxt.config.ts", "svelte.config.js",
        "wdio.conf.ts", "karma.conf.ci.js", "module/Playwright.CONFIG.CI.TS",
    ],
)
def test_new_build_and_config_proposals_are_rejected(tmp_path: Path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a();\n", encoding="utf-8")
    proposal = _proposal(path)

    errors = validate_proposal(proposal, tmp_path)
    assert errors and "не относится к коду автотестов" in errors[0]
    assert "РЕШЕНИЕ: не трогать" in errors[0] and "ПОЧЕМУ" in errors[0]
    result = apply_proposal(proposal, tmp_path, confirm=True, diff_hash="ignored")
    assert result.status == "error" and not result.changed
    assert target.read_text(encoding="utf-8") == "a();\n"


@pytest.mark.parametrize(
    "path",
    [
        "t.py", "T.java", "conftest.py", "tests/helpers/config.py",
        "src/test/java/Config.java", "tests/helpers/tool.config.ts", "tests/run.kts",
        "helpers/playwright_client.ts", "src/My Tests/Test.java",
    ],
)
def test_test_helpers_and_root_sources_remain_editable(tmp_path: Path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a();\n", encoding="utf-8")

    assert validate_proposal(_proposal(path), tmp_path) == []
    assert apply_proposal(_proposal(path), tmp_path).status == "diff"


@pytest.mark.parametrize("path", ["build.gradle.kts", "playwright.config.ts", "build/T.java"])
def test_previous_apply_to_now_denied_target_can_still_be_recognized_and_reverted(
    tmp_path: Path, path: str,
) -> None:
    proposal, files, target, original = _previous_apply(tmp_path, path)

    assert validate_proposal(proposal, tmp_path)
    assert applied_state(proposal, tmp_path, files) == "applied"
    already = apply_proposal(proposal, tmp_path, files=files, confirm=True)
    assert already.status == "applied" and not already.changed
    reverted = revert_proposal(tmp_path, files)
    assert reverted.status == "reverted"
    assert target.read_bytes() == original and not files.record.exists()
    assert apply_proposal(proposal, tmp_path, files=files).status == "error"


@pytest.mark.parametrize("damage", ["target", "backup"])
def test_historical_config_revert_keeps_file_and_backup_guards(tmp_path: Path, damage: str) -> None:
    _, files, target, _ = _previous_apply(tmp_path, "playwright.config.ts")
    (target if damage == "target" else files.backup).write_bytes(b"other changes\n")
    current = target.read_bytes()

    result = revert_proposal(tmp_path, files)
    assert result.status == "error" and not result.changed
    assert target.read_bytes() == current and files.record.exists()


def test_alias_to_config_is_also_denied(tmp_path: Path) -> None:
    target = tmp_path / "playwright.config.ts"
    target.write_text("a();\n", encoding="utf-8")
    (tmp_path / "test_order.ts").symlink_to(target)
    assert validate_proposal(_proposal("test_order.ts"), tmp_path)


def test_policy_does_not_change_source_index_skip_directories() -> None:
    assert "alla-kb" not in SKIP_DIRS and "buildSrc" not in SKIP_DIRS
