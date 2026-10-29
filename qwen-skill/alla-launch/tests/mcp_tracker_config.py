"""Конфигурация адаптера mcp.py для фейкового MCP-сервера: общая для тестов адаптера и tickets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

FAKE = Path(__file__).resolve().parent / "fake_mcp_server.py"


def config_text(db: Path, *server_args: str, extra: str = "", timeout: float = 10,
                create_arguments: str | None = None,
                projects: dict[str, str] | None = None, search_text: str = "{title_q}") -> str:
    command = json.dumps([sys.executable, str(FAKE), "--db", str(db), *server_args])
    arguments = create_arguments or ('{ project = "{project}", type = "{tracker_kind}", '
                                     'summary = "{title}", description = "{body}" }')
    project_lines = "\n".join(f"{json.dumps(team)} = {json.dumps(name)}"
                              for team, name in (projects or {"backend": "BACK", "qa": "QA"}).items())
    return f"""
site = "https://tracker.test"
[server]
command = {command}
timeout = {timeout}
[projects]
{project_lines}
[kinds]
bug = "Bug"
[create]
tool = "create_issue"
arguments = {arguments}
[create.result]
id_path = "key"
url_template = "https://tracker.test/browse/{{id}}"
[link]
tool = "add_comment"
arguments = {{ issue = "{{target_id}}", text = "{{body}}" }}
[link.result]
url_template = "https://tracker.test/browse/{{id}}"
[search]
tool = "search_issues"
arguments = {{ query = 'project = "{{project}}" AND text ~ "{search_text}"' }}
[search.result]
items_path = "issues"
id_path = "key"
state_path = "status"
url_template = "https://tracker.test/browse/{{id}}"
[lookup]
tool = "search_issues"
arguments = {{ query = 'project = "{{project}}" AND text ~ "{{request_id}}"' }}
[lookup.result]
items_path = "issues"
id_path = "key"
url_template = "https://tracker.test/browse/{{id}}"
verify_path = "description"
[lookup_link]
tool = "search_issues"
arguments = {{ query = 'project = "{{project}}" AND text ~ "{{request_id}}"' }}
[lookup_link.result]
items_path = "issues"
id_path = "key"
url_template = "https://tracker.test/browse/{{id}}"
{extra}
"""
