"""Реальные процессы адаптера mcp.py с фейковым MCP-сервером: протокол, метка и восстановление."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp_tracker_config import FAKE, config_text

ADAPTER = Path(__file__).resolve().parents[1] / "extensions" / "tickets" / "adapters" / "mcp.py"
EXAMPLE = ADAPTER.with_name("mcp-tracker.example.toml")
EXTENSIONS = ADAPTER.parents[2]
GUIDE = ADAPTER.parents[1] / "CONNECT-MCP.md"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path


def write_config(project: Path, *server_args: str, **options: Any) -> None:
    (project / "mcp-tracker.toml").write_text(
        config_text(project / "tracker.sqlite", *server_args, **options), encoding="utf-8")


def ticket(**updates: Any) -> dict[str, Any]:
    return {"kind": "bug", "team": "backend", "title": 'NPE при "создании" заказа',
            "description": "Фактический результат: ответ 500 вместо создания заказа.",
            "problems": [2, 5], "tests": [{"name": "createOrder", "link": "https://testops/2"}],
            "evidence": [{"problem": 2, "source_id": "S1", "quote": "NullPointerException"}],
            "launch_id": 777, "run_uid": str(uuid.UUID(int=7)), **updates}


def invoke(project: Path, request: dict[str, Any], *, env: dict[str, str] | None = None,
           pass_fds: tuple[int, ...] = (),
           config: str = "mcp-tracker.toml") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ADAPTER), "--config", config],
                          input=json.dumps(request, ensure_ascii=False), capture_output=True,
                          text=True, encoding="utf-8", timeout=60, cwd=project, pass_fds=pass_fds,
                          env={"PYTHONIOENCODING": "utf-8", **(env or {})})


def response(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.returncode == 0, process.stdout + process.stderr
    value: dict[str, Any] = json.loads(process.stdout)
    assert value["api"] == 1
    assert len(process.stdout.splitlines()) == 1
    return value


def prepared(project: Path, *, request_id: str | None = None, action: str = "create",
             target_id: str | None = None, **updates: Any) -> tuple[str, dict[str, Any]]:
    request_id = request_id or str(uuid.uuid4())
    result = response(invoke(project, {"api": 1, "operation": "prepare", "request_id": request_id,
                                       "action": action, "target_id": target_id,
                                       "ticket": ticket(**updates)}))
    return request_id, result


def mutation(request_id: str, draft: dict[str, Any], operation: str = "create") -> dict[str, Any]:
    return {"api": 1, "operation": operation, "request_id": request_id,
            "scope": draft["scope"], "payload": draft["payload"]}


def lookup(request_id: str, draft: dict[str, Any], action: str = "create") -> dict[str, Any]:
    return {"api": 1, "operation": "lookup", "request_id": request_id,
            "scope": draft["scope"], "action": action}


def search_request(request_id: str, draft: dict[str, Any], **updates: Any) -> dict[str, Any]:
    return {"api": 1, "operation": "search", "request_id": request_id, "scope": draft["scope"],
            "ticket": ticket(**updates), "action": "create", "target_id": None}


def issues(project: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(project / "tracker.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM issues ORDER BY key").fetchall()


def test_prepare_is_pure_deterministic_and_never_starts_the_server(project: Path) -> None:
    write_config(project)
    request_id = str(uuid.uuid4())
    _, first = prepared(project, request_id=request_id)
    _, again = prepared(project, request_id=request_id)
    _, other = prepared(project)
    assert first == again
    assert first["scope"] == other["scope"] == {"site": "https://tracker.test",
                                                "project_or_queue": "BACK"}
    assert first["capabilities"] == {"search": True, "authoritative_absence": False}
    assert not (project / "tracker.sqlite").exists()
    body = json.loads(base64.b64decode(first["payload"]))
    assert body["tool"] == "create_issue" and body["request_id"] == request_id
    assert body["arguments"]["type"] == "Bug"
    assert body["arguments"]["description"].endswith(f"\n\nalla-request: {request_id}")


def test_create_sends_stored_arguments_and_returns_receipt(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    result = response(invoke(project, mutation(request_id, draft)))
    assert result["outcome"] == "found"
    assert result["ticket"] == {"id": "TRK-1", "url": "https://tracker.test/browse/TRK-1",
                                "state": "open"}
    [row] = issues(project)
    stored = json.loads(base64.b64decode(draft["payload"]))["arguments"]
    assert (row["project"], row["type"], row["summary"], row["description"]) == (
        stored["project"], stored["type"], stored["summary"], stored["description"])


def test_repeated_delivery_returns_the_same_ticket_without_second_issue(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    first = response(invoke(project, mutation(request_id, draft)))
    second = response(invoke(project, mutation(request_id, draft)))
    assert first == second
    assert len(issues(project)) == 1


def test_lost_response_is_recovered_by_label_lookup(project: Path) -> None:
    write_config(project, "--lose-response-once")
    request_id, draft = prepared(project)
    lost = invoke(project, mutation(request_id, draft))
    assert lost.returncode == 1 and json.loads(lost.stdout)["error"]
    found = response(invoke(project, lookup(request_id, draft)))
    assert found["outcome"] == "found" and found["ticket"]["id"] == "TRK-1"
    assert len(issues(project)) == 1


def test_lookup_without_match_is_unknown_never_absent(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    assert response(invoke(project, lookup(request_id, draft)))["outcome"] == "unknown"
    response(invoke(project, mutation(request_id, draft)))
    other_id, other = prepared(project)
    assert response(invoke(project, lookup(other_id, other)))["outcome"] == "unknown"


def test_blind_index_after_lost_response_stays_unknown(project: Path) -> None:
    write_config(project, "--lose-response-once", "--search-blind")
    request_id, draft = prepared(project)
    assert invoke(project, mutation(request_id, draft)).returncode == 1
    assert response(invoke(project, lookup(request_id, draft)))["outcome"] == "unknown"
    assert len(issues(project)) == 1


def test_lookup_without_configured_tool_is_unknown_and_starts_no_server(project: Path) -> None:
    text = config_text(project / "tracker.sqlite")
    text = text[:text.index("[lookup]")]
    (project / "mcp-tracker.toml").write_text(text, encoding="utf-8")
    request_id, draft = prepared(project)
    assert response(invoke(project, lookup(request_id, draft)))["outcome"] == "unknown"
    assert not (project / "tracker.sqlite").exists()


def test_lookup_ignores_hit_whose_verified_field_lacks_the_label(project: Path) -> None:
    write_config(project)
    first_id, first = prepared(project)
    response(invoke(project, mutation(first_id, first)))
    link_id, link = prepared(project, action="link", target_id="TRK-1",
                             description="Новые доказательства.")
    response(invoke(project, mutation(link_id, link, "link")))
    # Поиск по метке link находит тикет через комментарий, но в его описании метки нет.
    assert response(invoke(project, lookup(link_id, link, "create")))["outcome"] == "unknown"
    assert response(invoke(project, lookup(link_id, link, "link")))["ticket"]["id"] == "TRK-1"


def test_link_adds_comment_with_label_and_recovers_lost_reply(project: Path) -> None:
    write_config(project)
    first_id, first = prepared(project)
    response(invoke(project, mutation(first_id, first)))
    write_config(project, "--lose-response-once")
    link_id, link = prepared(project, action="link", target_id="TRK-1",
                             description="Новые доказательства: запрос testops/5 с NPE.")
    assert invoke(project, mutation(link_id, link, "link")).returncode == 1
    found = response(invoke(project, lookup(link_id, link, "link")))
    assert found["ticket"]["id"] == "TRK-1"
    again = response(invoke(project, mutation(link_id, link, "link")))
    assert again["ticket"] == found["ticket"]
    [row] = issues(project)
    assert row["comments"].count(f"alla-request: {link_id}") == 1


def test_search_returns_only_stable_fields_in_the_ticket_project(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    response(invoke(project, mutation(request_id, draft)))
    result = response(invoke(project, search_request(request_id, draft)))
    assert result["matches"] == [{"id": "TRK-1", "url": "https://tracker.test/browse/TRK-1",
                                  "state": "Open", "reason": "Найдено поиском MCP трекера"}]
    other_id, other = prepared(project, team="qa")
    assert response(invoke(project, search_request(other_id, other, team="qa")))["matches"] == []


def test_search_escapes_quotes_in_the_title(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project, title='Ошибка "а\\б" при оплате')
    response(invoke(project, mutation(request_id, draft)))
    found = response(invoke(project, search_request(request_id, draft,
                                                    title='Ошибка "а\\б" при оплате')))
    assert [row["id"] for row in found["matches"]] == ["TRK-1"]


def test_search_scope_must_match_ticket_team(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    failure = invoke(project, search_request(request_id, draft, team="qa"))
    assert failure.returncode == 1 and json.loads(failure.stdout)["error"]


def test_search_failure_is_an_error_not_an_empty_list(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    text = (project / "mcp-tracker.toml").read_text(encoding="utf-8")
    (project / "mcp-tracker.toml").write_text(
        text.replace('items_path = "issues"\nid_path = "key"\nstate_path', 'items_path = "wrong"\n'
                     'id_path = "key"\nstate_path'), encoding="utf-8")
    failure = invoke(project, search_request(request_id, draft))
    assert failure.returncode == 1 and json.loads(failure.stdout)["error"]


def test_tracker_error_on_create_is_a_failure_without_server_text(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project, title="FAIL заказ")
    failure = invoke(project, mutation(request_id, draft))
    assert failure.returncode == 1
    assert "refused" not in failure.stdout + failure.stderr
    assert not issues(project)


def test_payload_must_match_operation_request_and_scope(project: Path) -> None:
    write_config(project)
    request_id, draft = prepared(project)
    _, other = prepared(project, team="qa")
    for request in (mutation(str(uuid.uuid4()), draft), mutation(request_id, draft, "link"),
                    {**mutation(request_id, draft), "scope": other["scope"]},
                    {**mutation(request_id, draft), "payload": "not-base64!"}):
        failure = invoke(project, request)
        assert failure.returncode == 1 and json.loads(failure.stdout)["error"]
    assert not (project / "tracker.sqlite").exists()


@pytest.mark.parametrize("updates", [{"kind": "invalid"}, {"team": "unknown-team"}, {"team": ""},
                                     {"kind": "investigation"}])
def test_invalid_preparation_is_refused(project: Path, updates: dict[str, Any]) -> None:
    write_config(project)
    failure = invoke(project, {"api": 1, "operation": "prepare", "request_id": str(uuid.uuid4()),
                               "action": "create", "target_id": None, "ticket": ticket(**updates)})
    assert failure.returncode == 1 and set(json.loads(failure.stdout)) == {"api", "error"}


def test_link_prepare_requires_target(project: Path) -> None:
    write_config(project)
    failure = invoke(project, {"api": 1, "operation": "prepare", "request_id": str(uuid.uuid4()),
                               "action": "link", "target_id": None, "ticket": ticket()})
    assert failure.returncode == 1


def test_arguments_without_body_are_refused_because_label_would_be_lost(project: Path) -> None:
    write_config(project, create_arguments='{ project = "{project}", type = "{tracker_kind}", '
                                           'summary = "{title}", description = "{title}" }')
    failure = invoke(project, {"api": 1, "operation": "prepare", "request_id": str(uuid.uuid4()),
                               "action": "create", "target_id": None, "ticket": ticket()})
    assert failure.returncode == 1 and "{body}" in json.loads(failure.stdout)["error"]


def test_unknown_placeholder_in_config_is_an_error(project: Path) -> None:
    write_config(project, create_arguments='{ summary = "{titel}", description = "{body}" }')
    failure = invoke(project, {"api": 1, "operation": "prepare", "request_id": str(uuid.uuid4()),
                               "action": "create", "target_id": None, "ticket": ticket()})
    assert failure.returncode == 1 and "titel" in json.loads(failure.stdout)["error"]


def test_missing_tool_on_server_is_refused_before_any_call(project: Path) -> None:
    write_config(project)
    text = (project / "mcp-tracker.toml").read_text(encoding="utf-8")
    (project / "mcp-tracker.toml").write_text(text.replace('tool = "create_issue"',
                                                           'tool = "make_issue"'), encoding="utf-8")
    request_id, draft = prepared(project)
    failure = invoke(project, mutation(request_id, draft))
    assert failure.returncode == 1 and "make_issue" in json.loads(failure.stdout)["error"]
    assert not issues(project)


def test_server_timeout_ends_the_call_with_an_error(project: Path) -> None:
    write_config(project, "--delay", "30", timeout=1)
    request_id, draft = prepared(project)
    started = time.monotonic()
    failure = invoke(project, mutation(request_id, draft))
    assert failure.returncode == 1 and time.monotonic() - started < 20


def test_server_gets_only_the_environment_the_adapter_received(project: Path) -> None:
    write_config(project, "--require-env", "TRACKER_TOKEN")
    request_id, draft = prepared(project)
    assert invoke(project, mutation(request_id, draft)).returncode == 1
    result = response(invoke(project, mutation(request_id, draft),
                             env={"TRACKER_TOKEN": "secret-value"}))
    assert "secret-value" not in json.dumps(result)


@pytest.mark.skipif(os.name != "posix", reason="наследование дескрипторов только на POSIX")
def test_mcp_server_inherits_the_journal_lock_descriptor(project: Path) -> None:
    probe = project / "probe.txt"
    lock = os.open(project / "lock", os.O_CREAT | os.O_RDWR)
    try:
        write_config(project, "--probe-fd", str(lock), str(probe))
        request_id, draft = prepared(project)
        response(invoke(project, mutation(request_id, draft), pass_fds=(lock,)))
    finally:
        os.close(lock)
    assert probe.read_text(encoding="utf-8") == "open"


def test_stdout_prefixed_noise_from_server_is_ignored(project: Path) -> None:
    wrapper = project / "noisy.py"
    wrapper.write_text("import sys,subprocess\nprint('npm notice hello', flush=True)\n"
                       "sys.exit(subprocess.call([sys.executable, sys.argv[1], *sys.argv[2:]]))\n",
                       encoding="utf-8")
    text = config_text(project / "tracker.sqlite")
    old = json.dumps([sys.executable, str(FAKE), "--db", str(project / "tracker.sqlite")])
    new = json.dumps([sys.executable, str(wrapper), str(FAKE), "--db", str(project / "tracker.sqlite")])
    (project / "mcp-tracker.toml").write_text(text.replace(old, new), encoding="utf-8")
    request_id, draft = prepared(project)
    assert response(invoke(project, mutation(request_id, draft)))["ticket"]["id"] == "TRK-1"


def test_invalid_config_fails_closed_for_every_operation(project: Path) -> None:
    (project / "mcp-tracker.toml").write_text("site = 1\n", encoding="utf-8")
    for operation in ("prepare", "search", "create", "link", "lookup"):
        process = invoke(project, {"api": 1, "operation": operation,
                                   "request_id": str(uuid.uuid4())})
        assert process.returncode == 1
        assert set(json.loads(process.stdout)) == {"api", "error"}


def test_unknown_server_command_is_a_clear_error(project: Path) -> None:
    write_config(project)
    text = (project / "mcp-tracker.toml").read_text(encoding="utf-8")
    start = text.index("command = ")
    end = text.index("\n", start)
    (project / "mcp-tracker.toml").write_text(
        text[:start] + 'command = ["no-such-mcp-server-binary"]' + text[end:], encoding="utf-8")
    request_id, draft = prepared(project)
    failure = invoke(project, mutation(request_id, draft))
    assert failure.returncode == 1 and "MCP-сервера" in json.loads(failure.stdout)["error"]


def test_example_config_is_valid_and_prepares_a_ticket(project: Path) -> None:
    (project / "mcp-tracker.toml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    _, draft = prepared(project)
    assert draft["scope"] == {"site": "https://tracker.company", "project_or_queue": "BACK"}


def guide_examples() -> list[str]:
    text = GUIDE.read_text(encoding="utf-8")
    return re.findall(r"<!-- example: mcp-tracker -->\n```toml\n(.*?)```", text, flags=re.S)


@pytest.mark.parametrize("index", range(2))
def test_connect_guide_examples_are_valid_and_prepare_a_ticket(project: Path, index: int) -> None:
    examples = guide_examples()
    assert len(examples) == 2
    (project / "mcp-tracker.toml").write_text(examples[index], encoding="utf-8")
    _, draft = prepared(project)
    assert draft["scope"]["project_or_queue"] in {"BACK", "team/autotests"}
    assert json.loads(base64.b64decode(draft["payload"]))["arguments"]


def test_connect_guide_error_table_quotes_real_adapter_messages() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    table = text.split("<!-- adapter-errors -->", 1)[1].split("\n\n", 1)[0]
    fragments = re.findall(r"^\| `([^`]+)` \|", table, flags=re.M)
    assert len(fragments) >= 15
    source = ADAPTER.read_text(encoding="utf-8")
    assert [item for item in fragments if item not in source] == []


def test_connect_guide_probes_work_against_the_fake_server(project: Path) -> None:
    write_config(project)
    text = GUIDE.read_text(encoding="utf-8")
    probes = re.findall(r"printf '%s' '(\{.*?\})' \|", text)
    assert [json.loads(item)["operation"] for item in probes] == ["prepare", "search", "lookup"]
    prepare_probe, search_probe, lookup_probe = (json.loads(item) for item in probes)
    config = (project / "mcp-tracker.toml").read_text(encoding="utf-8")
    (project / "mcp-tracker.toml").write_text(
        config.replace("https://tracker.test", "https://tracker.company")
        .replace("[projects]\n", '[projects]\ntest = "TEST"\n'), encoding="utf-8")
    assert response(invoke(project, prepare_probe))["scope"] == search_probe["scope"] == {
        "site": "https://tracker.company", "project_or_queue": "TEST"}
    assert response(invoke(project, search_probe))["matches"] == []
    assert lookup_probe["scope"] == search_probe["scope"]
    lookup_probe["request_id"] = prepare_probe["request_id"]
    assert response(invoke(project, lookup_probe))["outcome"] == "unknown"


def test_connect_guide_tool_check_snippet_lists_server_tools(project: Path) -> None:
    write_config(project)
    text = GUIDE.read_text(encoding="utf-8")
    found = re.search(r"<<'EOF'\n(.*?)\n\s*EOF\n", text, flags=re.S)
    assert found is not None
    script = textwrap.dedent(found.group(1))
    config = project / "mcp-tracker.toml"
    ok = subprocess.run([sys.executable, "-", str(config)], input=script, capture_output=True,
                        text=True, cwd=project, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert "[create] create_issue: есть" in ok.stdout and "НЕТ" not in ok.stdout
    config.write_text(config.read_text(encoding="utf-8").replace(
        'tool = "create_issue"', 'tool = "creat_issue"'), encoding="utf-8")
    bad = subprocess.run([sys.executable, "-", str(config)], input=script, capture_output=True,
                         text=True, cwd=project, timeout=60)
    assert "[create] creat_issue: НЕТ У СЕРВЕРА" in bad.stdout


@pytest.mark.parametrize("name", ["README.md", "AUTHORING.md", "tickets/README.md",
                                  "tickets/CONNECT-MCP.md"])
def test_extension_guides_link_to_existing_files(name: str) -> None:
    path = EXTENSIONS / name
    for target in re.findall(r"\]\((?!https?:|#)([^)#\s]+)", path.read_text(encoding="utf-8")):
        assert (path.parent / target).exists(), f"{name}: {target}"
