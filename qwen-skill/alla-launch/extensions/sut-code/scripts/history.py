"""Действие ``history`` расширения sut-code: кто и когда менял место в коде.

``… action sut-code history --context … -- <путь>[:строка]`` (при нескольких репозиториях —
``<репозиторий>:<путь>[:строка]``). Со строкой — последние изменения этих строк
(``git log -L``), без — последние коммиты файла. История — от выбранной ревизии назад.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sutlib

COMMITS = 5
OUTPUT_LINES = 120


def history(context: dict[str, Any], context_file: Path) -> list[str]:
    state = sutlib.load_state(context_file)
    tree, path, line = sutlib.split_location(sutlib.action_args(), state)
    git_dir = Path(tree["git_dir"])
    sha = tree["sha"]
    if line:
        start, end = max(1, line - 3), line + 3
        result = sutlib.git(git_dir, "log", f"-n{COMMITS}", "--format=commit %h %ad %an: %s",
                            "--date=short", f"-L{start},{end}:{path}", sha,
                            timeout=sutlib.NETWORK_TIMEOUT, check=False)
    else:
        result = sutlib.git(git_dir, "log", f"-n{COMMITS * 2}", "--format=%h %ad %an: %s",
                            "--date=short", sha, "--", path, timeout=sutlib.NETWORK_TIMEOUT,
                            check=False)
    if result.returncode != 0:
        return [f"Истории {path} нет: {sutlib.tail(result.stderr) or 'файл не найден в ревизии'}"]
    rows = result.stdout.rstrip().splitlines()
    if not rows:
        return [f"У {path} в ревизии {sha[:8]} нет истории изменений."]
    cut = ["… (обрезано)"] if len(rows) > OUTPUT_LINES else []
    where = f"{path}:{line}" if line else path
    return [f"История {where} до ревизии {sha[:8]} (новые сверху):",
            *(row[:200] for row in rows[:OUTPUT_LINES]), *cut]


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(history))
