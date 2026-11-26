"""Native Qwen environment loading must not disguise plaintext secrets as CI exports."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

from qwen_credentials_support import (
    SYNTHETIC_TOKEN, QwenCredentialUnavailable, isolated_qwen_env, native_qwen_package,
)


@pytest.mark.parametrize("source", ["home_dotenv", "settings_env", "exported"])
def test_native_qwen_environment_provenance_reaches_settings(
    tmp_path: Path, source: str,
) -> None:
    if os.name == "nt":
        pytest.skip("native Qwen credential fixture requires POSIX")
    try:
        package = native_qwen_package()
    except QwenCredentialUnavailable as exc:
        pytest.skip(str(exc))
    # Locate the native environment loader by its definition, without binding a
    # regression test to a generated chunk filename or importing the CLI entry.
    loaders = [path for path in (package / "chunks").glob("*.js")
               if "function loadEnvironment(" in path.read_text(encoding="utf-8")]
    assert len(loaders) == 1
    project = tmp_path / "project with spaces"
    project.mkdir()
    env = isolated_qwen_env(tmp_path)
    settings: dict[str, object] = {"security": {"folderTrust": {"enabled": False}}}
    if source == "home_dotenv":
        qwen_home = Path(env["QWEN_HOME"])
        qwen_home.mkdir()
        (qwen_home / ".env").write_text(f"ALLURE_TOKEN={SYNTHETIC_TOKEN}\n")
    else:
        settings["env"] = {"ALLURE_TOKEN": SYNTHETIC_TOKEN}
        if source == "exported":
            env["ALLURE_TOKEN"] = SYNTHETIC_TOKEN
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    # Python receives precisely the environment created by real Qwen. It emits
    # observations only; neither the token nor environment contents reach logs.
    python_code = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(scripts)!r})\n"
        "from alla_core.config import Settings\n"
        "from alla_core.exceptions import ConfigurationError\n"
        "try:\n"
        "    settings = Settings.load(require_testops=False)\n"
        "    outcome = {'accepted': bool(settings.token), 'source_rejected': False}\n"
        "except ConfigurationError as error:\n"
        "    outcome = {'accepted': False, 'source_rejected': 'получен Qwen' in str(error)}\n"
        "print(json.dumps(outcome))\n"
    )
    javascript = (
        f"import {{loadEnvironment}} from {json.dumps(loaders[0].as_uri())};\n"
        "import {spawnSync} from 'node:child_process';\n"
        f"loadEnvironment({json.dumps(settings)}, process.cwd());\n"
        f"const child = spawnSync({json.dumps(sys.executable)}, "
        f"['-B', '-c', {json.dumps(python_code)}], "
        "{env: process.env, encoding: 'utf8'});\n"
        "process.stdout.write(child.stdout || '');\n"
        "process.stderr.write(child.stderr || '');\n"
        "process.exitCode = child.status ?? 1;\n"
    )
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "--input-type=module", "-e", javascript],
                            cwd=project, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert SYNTHETIC_TOKEN not in result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed == {"accepted": source == "exported", "source_rejected": source != "exported"}
