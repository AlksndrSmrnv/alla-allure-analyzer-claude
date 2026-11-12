"""Compatibility depends on installed capabilities, not a pinned Qwen version."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

from alla_core import credentials
from qwen_credentials_support import SYNTHETIC_TOKEN, isolated_qwen_env


@pytest.mark.parametrize("hint", ["QWEN_CODE_CLI", "QWEN_CLI_ENTRY"])
@pytest.mark.parametrize("entry", ["cli-entry.js", "dist/cli.js", "bin/qwen.js"])
def test_active_qwen_is_found_without_cli_in_path(tmp_path: Path, hint: str, entry: str) -> None:
    package = tmp_path / "existing Qwen"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"name": "@qwen-code/qwen-code", "version": "9.1.0"}))
    executable = package / entry
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("// existing runtime entry; never executed\n")
    assert credentials.qwen_package({hint: str(executable), "PATH": ""}) == package


def test_npm_shim_prefers_adjacent_installation_over_ancestor_package(tmp_path: Path) -> None:
    ancestor = tmp_path / "prefix"
    actual = ancestor / "npm/node_modules/@qwen-code/qwen-code"
    actual.mkdir(parents=True)
    metadata = json.dumps({"name": "@qwen-code/qwen-code", "version": "1.0.0"})
    (ancestor / "package.json").write_text(metadata)
    (actual / "package.json").write_text(metadata)
    shim = ancestor / "npm/qwen"
    shim.write_text("// npm shim\n")
    assert credentials.qwen_package({"QWEN_CODE_CLI": str(shim), "PATH": ""}) == actual


@pytest.mark.parametrize("version", ["0.26.0", "0.28.0", "1.0.0", "0.27.0-preview"])
@pytest.mark.parametrize("layout", ["chunks", "dist/chunks", "both", "legacy-root"])
def test_compatible_reader_accepts_other_versions(tmp_path: Path, version: str, layout: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable for reader protocol probe")
    package = tmp_path / "installed"
    chunks = package / ("chunks" if layout in {"both", "legacy-root"} else layout)
    chunks.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({
        "name": "@qwen-code/qwen-code", "version": version, "type": "module"}))
    # A protocol fixture, not a replacement for encryption tests against real Qwen.
    (chunks / "reader.js").write_text('''
class ExtensionManager {
  async loadExtensionManifestHead() {
    return {loadedManifest: {format: 'qwen'}, extension: {
      name: 'alla-launch-credentials', id: 'synthetic', config: {settings: [
        {envVar: 'ALLURE_TOKEN', sensitive: true}
      ]}
    }};
  }
}
async function getScopedEnvContents(config, id, scope) {
  if (config.settings.length === 0 || scope !== 'workspace') return {};
  return {ALLURE_TOKEN: ''' + json.dumps(SYNTHETIC_TOKEN) + '''};
}
export { ExtensionManager, getScopedEnvContents };
''')
    if layout in {"both", "legacy-root"}:
        (package / "dist/chunks").mkdir(parents=True)
        shutil.copyfile(chunks / "reader.js", package / "dist/chunks/reader.js")
        if layout == "legacy-root":
            reader = chunks / "reader.js"
            reader.write_text(reader.read_text().replace("loadExtensionManifestHead", "legacyManifestHead"))
    env = isolated_qwen_env(tmp_path)
    extension = Path(env["QWEN_HOME"]) / "extensions" / credentials.EXTENSION_NAME
    extension.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    result = subprocess.run([node, str(credentials.BRIDGE), str(package), str(project)],
                            cwd=package, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"token": SYNTHETIC_TOKEN, "source": "qwen:workspace"}
    assert result.stderr == ""


def test_missing_capabilities_report_interface_not_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "qwen_package", lambda _: tmp_path)
    monkeypatch.setattr(credentials.shutil, "which", lambda *args, **kwargs: "/node")
    monkeypatch.setattr(credentials.subprocess, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess([], 1, "", "QWEN_UNSUPPORTED\n"))
    from alla_core.exceptions import ConfigurationError
    with pytest.raises(ConfigurationError, match="интерфейс") as error:
        credentials.load_qwen_token(tmp_path, {"PATH": "/bin"})
    assert "0.27" not in str(error.value)
    assert "установите" not in str(error.value)
