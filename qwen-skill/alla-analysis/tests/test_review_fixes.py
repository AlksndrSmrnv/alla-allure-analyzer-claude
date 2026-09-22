import asyncio
import os
import stat
import subprocess

import pytest

from alla_skill.config import Settings
from alla_skill.models.testops import FailedTestSummary, TestResultResponse as Result
from alla_skill.services.triage_service import TriageService
from alla_skill.services.log_extraction_service import _detect_content_type
from alla_skill.workflow import source_text
from .test_workflow import prepared, finish_payloads
from alla_skill.workflow import finalize


@pytest.mark.parametrize(
    "endpoint",
    ["https://:secret@example.com", "https://user:secret@example.com", "https://@example.com"],
)
def test_no_url_credentials(endpoint):
    with pytest.raises(ValueError):
        Settings(endpoint=endpoint, token="token")


@pytest.mark.parametrize(
    "name",
    [
        "config/prod.env",
        ".env.py",
        "config/prod.env.py",
        "config/private.pem",
        "config/secret.key",
        ".ssh/id_rsa",
        "config/credentials.json",
    ],
)
def test_code_sources_reject_secret_files(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("very-sensitive-content")
    with pytest.raises(ValueError):
        source_text({"project_root": str(tmp_path)}, tmp_path, f"code:{name}:1")


def test_code_source_symlink_does_not_bypass_filter(tmp_path):
    (tmp_path / "prod.env").write_text("sensitive")
    (tmp_path / "fixture.py").symlink_to(tmp_path / "prod.env")
    with pytest.raises(ValueError):
        source_text({"project_root": str(tmp_path)}, tmp_path, "code:fixture.py:1")


def test_binary_without_nul_is_not_text():
    assert _detect_content_type(b"\x01\x02\x03\x04\x05" * 500) == "binary"
    assert _detect_content_type(b"GIF89a\xff\xff", fallback_mime="text/plain") == "binary"
    assert _detect_content_type("Ошибка запроса".encode(), fallback_mime="text/plain") == "text"


def test_optional_detail_fetch_budget():
    class Provider:
        calls = []
        sources = {}

        async def get_test_result_detail(self, tid):
            self.calls.append(tid)
            return Result(id=tid, trace="useful trace")

    provider = Provider()
    settings = Settings(endpoint="https://test.test", token="token", max_detail_enrichments=2)
    summaries = [
        FailedTestSummary(
            test_result_id=i,
            name="test",
            status="failed",
            status_message="message" if i < 4 else None,
        )
        for i in range(5)
    ]
    asyncio.run(TriageService(provider, settings)._fetch_missing_traces(summaries))
    assert sorted(provider.calls) == [0, 1, 4]
    assert provider.sources["detail:2"]["state"] == "skipped"
    assert summaries[4].status_trace == "useful trace"


def test_artifact_modes(tmp_path):
    old = os.umask(0)
    try:
        directory = prepared(tmp_path)
        finish_payloads(directory)
        finalize(directory)
    finally:
        os.umask(old)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in directory.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path


def test_settings_avoid_root_dotenv_and_load_explicit_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLURE_TOKEN", raising=False)
    monkeypatch.delenv("ALLURE_ENDPOINT", raising=False)
    (tmp_path / ".env").write_text("ALLURE_ENDPOINT=https://test.test\nALLURE_TOKEN=root-secret\n")
    with pytest.raises(ValueError):
        Settings.load(tmp_path)
    settings = Settings.load(tmp_path, env_file=tmp_path / ".env")
    assert settings.token == "root-secret"
    custom = tmp_path / "private.env"
    custom.write_text(
        "ALLURE_ENDPOINT=https://test.test\nALLURE_TOKEN=private-secret\nALLURE_CLUSTERING_THRESHOLD=0.8\nALLURE_LOGS_CLUSTERING_WEIGHT=0.0\nALLURE_MAX_DETAIL_ENRICHMENTS=5\n"
    )
    settings = Settings.load(tmp_path, env_file=custom)
    assert settings.clustering_threshold == 0.8
    assert settings.logs_clustering_weight == 0
    assert settings.max_detail_enrichments == 5


def test_tracked_env_file_is_rejected(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / ".env"
    path.write_text("ALLURE_ENDPOINT=https://test.test\nALLURE_TOKEN=tracked-secret\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env"], check=True)
    with pytest.raises(ValueError, match="Git"):
        Settings.load(tmp_path, env_file=path)


def test_invalid_snapshot_does_not_chmod_arbitrary_directory(tmp_path):
    from alla_skill.workflow import context

    file = tmp_path / "source.py"
    file.write_text("print(1)")
    file.chmod(0o644)
    with pytest.raises(OSError):
        context(tmp_path)
    assert stat.S_IMODE(file.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "field,value",
    [
        ("clustering_threshold", float("nan")),
        ("clustering_threshold", 2),
        ("logs_clustering_weight", -1),
        ("max_detail_enrichments", -1),
    ],
)
def test_invalid_analysis_settings_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(endpoint="https://test.test", token="token", **{field: value})


def test_zero_budget_retains_required_fallback():
    class Provider:
        calls = []
        sources = {}

        async def get_test_result_detail(self, tid):
            self.calls.append(tid)
            return Result(id=tid, statusDetails={"message": "from details"})

    provider = Provider()
    summaries = [
        FailedTestSummary(
            test_result_id=i, name="test", status="failed", status_message="known" if i else None
        )
        for i in range(2)
    ]
    asyncio.run(
        TriageService(
            provider,
            Settings(endpoint="https://test.test", token="token", max_detail_enrichments=0),
        )._fetch_missing_traces(summaries)
    )
    assert provider.calls == [0]
    assert summaries[0].status_message == "from details"


def test_skipped_and_empty_attachment_are_distinct():
    import httpx
    from alla_skill.client import Client
    from .test_workflow import transport

    def handler(request):
        if request.url.path.endswith("/20/content"):
            return httpx.Response(200, content=b"")
        if request.url.path.endswith("/21/content"):
            return httpx.Response(200, content=b"GIF89a\xff\xff")
        return transport(request)

    async def run():
        async with Client(
            Settings(endpoint="https://test.test", token="token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get_attachments_for_test_result(1)
            assert client.sources["attachment:20"]["state"] == "skipped"
            await client.get_attachment_content(20)
            await client.get_attachment_content(21)
            assert client.sources["attachment:20"]["state"] == "absent"
            assert client.sources["attachment:21"]["state"] == "skipped"

    asyncio.run(run())


@pytest.mark.parametrize("value", [42, True, ["oops"], {"nested": "oops"}])
def test_detail_values_must_be_strings(value):
    class Provider:
        async def get_test_result_detail(self, tid):
            return Result(id=tid, statusDetails={"message": value, "trace": value})

    summary = FailedTestSummary(test_result_id=1, name="test", status="failed")
    asyncio.run(
        TriageService(
            Provider(), Settings(endpoint="https://test.test", token="token")
        )._fetch_missing_traces([summary])
    )
    assert summary.status_message is None
    assert summary.status_trace is None


@pytest.mark.parametrize("suffix", ["yaml", "yml", "toml", "properties", "ini"])
def test_configuration_evidence(tmp_path, suffix):
    path = tmp_path / f"config.{suffix}"
    path.write_text('timeout = 3000\ndb.password = "sensitive value"\n')
    text = source_text({"project_root": str(tmp_path)}, tmp_path, f"code:{path.name}:1")
    assert "timeout = 3000" in text
    assert "sensitive" not in text


@pytest.mark.parametrize(
    "content",
    [
        "password: |\n  sensitive\n",
        'token = """\nsensitive\n"""\n',
        "password = first\\\nsensitive\n",
        "credentials:\n  value: sensitive\n",
        'password: "first\nsensitive"\n',
    ],
)
def test_multiline_config_secrets_cannot_be_read_by_offset(tmp_path, content):
    (tmp_path / "config.yaml").write_text(content)
    with pytest.raises(ValueError):
        source_text({"project_root": str(tmp_path)}, tmp_path, "code:config.yaml:2")


def test_snapshot_symlink_error_identifies_link_without_touching_target(tmp_path):
    from alla_skill.evidence import secure_artifacts

    target = tmp_path / "outside.txt"
    target.write_text("data")
    target.chmod(0o644)
    run = tmp_path / "snapshot"
    run.mkdir()
    (run / "bad-link").symlink_to(target)
    with pytest.raises(ValueError, match="bad-link"):
        secure_artifacts(run)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("value", [42, True, ["oops"], {"nested": "oops"}])
def test_invalid_status_details_in_steps_and_results(value):
    from alla_skill.models.testops import ExecutionStep

    service = TriageService(object(), Settings(endpoint="https://test.test", token="token"))
    step = ExecutionStep(
        name="step", status="failed", statusDetails={"message": value, "trace": value}
    )
    assert service._extract_error_from_step(step) == (None, None)
    result = Result(id=1, status="failed", statusDetails={"message": value, "trace": value})
    summary = service._build_failed_summary(result, [step], 123)
    assert summary.status_message is None
    assert summary.status_trace is None


def test_valid_detail_text_is_preserved_when_other_value_is_invalid():
    class Provider:
        async def get_test_result_detail(self, tid):
            return Result(
                id=tid,
                statusDetails={
                    "message": {"invalid": True},
                    "trace": "AssertionError: actual=503\nCaused by: timeout",
                },
            )

    summary = FailedTestSummary(test_result_id=1, name="test", status="failed")
    asyncio.run(
        TriageService(
            Provider(), Settings(endpoint="https://test.test", token="token")
        )._fetch_missing_traces([summary])
    )
    assert summary.status_message == "AssertionError: actual=503"
    assert summary.status_trace.endswith("Caused by: timeout")


@pytest.mark.parametrize(
    "key", ["token_endpoint_auth_method", "passwordless", "secretManagerUrl", "cookieName"]
)
def test_nonsecret_config_keys_remain_evidence(tmp_path, key):
    path = tmp_path / "config.yaml"
    path.write_text(f"{key}: useful-value\n")
    assert f"{key}: useful-value" in source_text(
        {"project_root": str(tmp_path)}, tmp_path, "code:config.yaml:1"
    )


def test_nonsecret_sections_and_list_neighbors_remain_evidence(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "tokens:\n  enabled: true\ncookies:\n  enabled: false\nitems:\n  - password: sensitive\n    timeout: 30\n"
    )
    text = source_text({"project_root": str(tmp_path)}, tmp_path, "code:config.yaml:1")
    assert "timeout: 30" in text
    assert "sensitive" not in text


@pytest.mark.parametrize("value", ["", "garbage"])
def test_numeric_configuration_error_names_variable(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ALLURE_CLUSTERING_THRESHOLD", value)
    with pytest.raises(ValueError, match="ALLURE_CLUSTERING_THRESHOLD") as error:
        Settings.load(tmp_path)
    assert "garbage" not in str(error.value)


@pytest.mark.parametrize("key", ["access_token", "clientSecret", "x-api-key", "db.password"])
def test_normalized_secret_names_are_masked(tmp_path, key):
    (tmp_path / "config.toml").write_text(f'{key} = "sensitive"\n')
    text = source_text({"project_root": str(tmp_path)}, tmp_path, "code:config.toml:1")
    assert "sensitive" not in text
    assert "[REDACTED]" in text


def test_env_templates_are_not_ignored(tmp_path):
    from pathlib import Path
    import shutil

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shutil.copyfile(Path(__file__).parents[1] / ".gitignore", tmp_path / ".gitignore")
    for name in [".env.example", ".env.local.example"]:
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", name])
        assert result.returncode == 1
    for name in [".env", ".env.local"]:
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", name])
        assert result.returncode == 0
