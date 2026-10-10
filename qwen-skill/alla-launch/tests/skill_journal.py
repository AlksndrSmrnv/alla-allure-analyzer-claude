"""Построитель журналов сеанса Qwen Code (формат 0.25) для тестов проверки разбора.

Записи повторяют настоящие журналы стенда: ``user`` (``provenance: real_user``),
``assistant`` с ``functionCall``/текстом/``usageMetadata``, ``tool_result`` с
``functionResponse`` и ``toolCallResult``, ``system``/``ui_telemetry``. У shell результат
в ``response.output`` обёрнут строками ``Command:``/``Directory:``/``Output:``, чистый вывод
— в ``resultDisplay.output``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SKILL = "python3 .qwen/skills/alla-launch/scripts/alla_skill.py"
START = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


@dataclass
class Clock:
    """Общие часы журналов одного теста: записи субагента ложатся между записями сеанса."""

    now: datetime = START

    def tick(self) -> str:
        self.now += timedelta(seconds=1)
        return self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Journal:
    session: str
    project: Path
    clock: Clock = field(default_factory=Clock)
    records: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0

    def _stamp(self) -> str:
        return self.clock.tick()

    def _record(self, kind: str, **extra: Any) -> dict[str, Any]:
        record = {"sessionId": self.session, "timestamp": self._stamp(), "type": kind,
                  "cwd": str(self.project), "version": "0.25.0", **extra}
        self.records.append(record)
        return record

    def user(self, text: str) -> None:
        self._record("user", provenance="real_user",
                     message={"role": "user", "parts": [{"text": text}]})

    def text(self, text: str, *, thought: bool = False, tokens: int = 100) -> None:
        part: dict[str, Any] = {"text": text}
        if thought:
            part["thought"] = True
        self._record("assistant", model="qwen/qwen3.8-flash", provenance="assistant_output",
                     message={"role": "model", "parts": [part]},
                     usageMetadata={"promptTokenCount": tokens, "candidatesTokenCount": 10,
                                    "thoughtsTokenCount": 5, "totalTokenCount": tokens + 15,
                                    "cachedContentTokenCount": tokens // 2})

    def call(self, name: str, args: dict[str, Any], output: str, *, error: bool = False,
             tokens: int = 100) -> None:
        self.calls += 1
        call_id = f"call_{self.session[:4]}_{self.calls}"
        self._record("assistant", model="qwen/qwen3.8-flash", provenance="assistant_output",
                     message={"role": "model",
                              "parts": [{"functionCall": {"id": call_id, "name": name, "args": args}}]},
                     usageMetadata={"promptTokenCount": tokens, "candidatesTokenCount": 10,
                                    "thoughtsTokenCount": 0, "totalTokenCount": tokens + 10,
                                    "cachedContentTokenCount": 0})
        if name == "run_shell_command":
            wrapped = (f"Command: {args.get('command', '')}\nDirectory: (root)\nOutput: {output}\n"
                       f"Error: (none)\nExit Code: {1 if error else 0}\nSignal: (none)")
            response = {"error" if error else "output": wrapped}
            display: Any = {"type": "shell_result", "version": 1, "exitCode": 1 if error else 0,
                            "output": output}
        else:
            response = {"error" if error else "output": output}
            display = ""
        self._record("tool_result", provenance="tool_result",
                     message={"role": "user", "parts": [
                         {"functionResponse": {"id": call_id, "name": name, "response": response}}]},
                     toolCallResult={"callId": call_id, "status": "error" if error else "success",
                                     "resultDisplay": display})

    def skill(self, command: str, output: str, **kwargs: Any) -> None:
        self.call("run_shell_command", {"command": f"{SKILL} {command}"}, output, **kwargs)

    def api_error(self) -> None:
        self._record("system", subtype="ui_telemetry", provenance="system",
                     systemPayload={"uiEvent": {"event.name": "qwen-code.api_error",
                                                "error_message": "Request timeout after 45s."}})

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.records),
                        encoding="utf-8")
        return path


def qwen_dirs(tmp: Path, project: Path) -> Path:
    """Папка проекта в ``QWEN_HOME/projects`` (как вычисляет Qwen)."""
    from alla_skill_lib.session_log import sanitize_cwd

    return tmp / "qwen-home" / "projects" / sanitize_cwd(str(project))


def save_main(journal: Journal, project_dir: Path) -> Path:
    return journal.write(project_dir / "chats" / f"{journal.session}.jsonl")


def save_subagent(journal: Journal, project_dir: Path, parent: str, agent_id: str) -> Path:
    folder = project_dir / "subagents" / parent
    (folder / f"agent-{agent_id}.meta.json").parent.mkdir(parents=True, exist_ok=True)
    (folder / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentId": agent_id, "parentSessionId": parent, "agentType": "alla-batch"}),
        encoding="utf-8")
    return journal.write(folder / f"agent-{agent_id}.jsonl")
