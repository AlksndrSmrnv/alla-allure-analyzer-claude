"""Run with an isolated Python containing requirements.txt and no installed Alla."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    assert importlib.util.find_spec("alla") is None, "Use an isolated Python without Alla"
    project = Path(tempfile.mkdtemp(prefix="alla-project-"))
    source = Path(__file__).resolve().parents[1]
    skill = project / ".qwen" / "skills" / "alla-analysis"
    shutil.copytree(
        source,
        skill,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "tests"
        ),
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_payment.py").write_text(
        "def test_payment(response):\n    assert response.status_code == 200\n"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            assert self.path == "/api/uaa/oauth/token"
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.reply({"access_token": "synthetic-jwt"})

        def do_GET(self):
            if self.path == "/api/launch/12345":
                body = {"id": 12345, "name": "Synthetic regression", "projectId": 7, "closed": True}
            elif self.path.startswith("/api/testresult?"):
                body = {
                    "content": [
                        {
                            "id": 1,
                            "name": "test_payment",
                            "fullName": "tests.test_payment",
                            "status": "failed",
                            "statusDetails": {
                                "message": "Expected 200 but was: <500>",
                                "trace": "AssertionError at tests/test_payment.py:2",
                            },
                        }
                    ],
                    "totalElements": 1,
                    "last": True,
                }
            elif self.path.endswith("/execution"):
                body = []
            elif self.path.startswith("/api/testresult/attachment?"):
                body = {"content": [], "last": True, "totalElements": 0}
            else:
                self.send_error(404)
                return
            self.reply(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        "ALLURE_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
        "ALLURE_TOKEN": "synthetic-api-token",
    }
    env.pop("PYTHONPATH", None)
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(key)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"

    def command(*args):
        result = subprocess.run(
            [sys.executable, str(skill / "scripts" / "alla.py"), *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        return payload

    try:
        prepared = command("prepare", "12345", "--project-root", str(project))
        directory = Path(prepared["run_dir"])
        cid = prepared["pending"][0]
        ctx = command("context", str(directory), cid)
        assert ctx["examples"][0]["status_message"] == "Expected 200 but was: <500>"
        (directory / "analyses" / f"{cid}.json").write_text(
            json.dumps(
                {
                    "run_id": prepared["run_id"],
                    "cluster_id": cid,
                    "symptom": "Получен HTTP 500 вместо 200.",
                    "cause": "Причина не установлена.",
                    "category": "неизвестно",
                    "confidence": "низкая",
                    "evidence": [
                        {"source": "test:1:status_message", "quote": "Expected 200 but was: <500>"},
                        {
                            "source": "code:tests/test_payment.py:2",
                            "quote": "assert response.status_code == 200",
                        },
                    ],
                    "limitations": ["Нет логов приложения."],
                    "contradictions": [],
                    "next_action": "Получить лог запроса.",
                    "code_alignment": "неизвестно",
                    "code_alignment_reason": "В прогоне нет revision.",
                }
            )
        )
        assert command("context", str(directory))["pending"] == []
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": prepared["run_id"],
                    "summary_lines": [
                        "Разобрано одно активное падение.",
                        "Причина требует проверки серверного ответа.",
                    ],
                    "priority_actions": ["Получить лог запроса."],
                    "findings": [],
                }
            )
        )
        final = command("finalize", str(directory))
        assert final["summary"] in Path(final["report"]).read_text()
        assert "test_payment.py:2" in Path(final["report"]).read_text()
        print(
            json.dumps(
                {
                    "ok": True,
                    "installed_alla": False,
                    "project": str(project),
                    "report": final["report"],
                    "note": "Synthetic model JSON, not a live Qwen evaluation",
                },
                ensure_ascii=False,
            )
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
