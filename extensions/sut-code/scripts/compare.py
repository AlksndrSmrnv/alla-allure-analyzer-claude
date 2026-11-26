"""Действие ``compare`` расширения sut-code: менялся ли файл в основной ветке после ревизии.

``… action sut-code compare --context … -- <путь>`` — коммиты основной ветки после
выбранной ревизии, которые трогали файл: возможно, ошибку уже исправили.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sutlib

COMMITS = 10


def compare(context: dict[str, Any], context_file: Path) -> list[str]:
    state = sutlib.load_state(context_file)
    tree, path, _ = sutlib.split_location(sutlib.action_args(), state)
    git_dir = Path(tree["git_dir"])
    branch = tree["default_branch"]
    head = sutlib.git(git_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                      check=False).stdout.strip()
    if not head:
        return [f"Основной ветки {branch} в копии нет — сравнить не с чем."]
    if head == tree["sha"]:
        return [f"Выбранная ревизия — это и есть последний коммит ветки {branch}."]
    result = sutlib.git(git_dir, "log", f"-n{COMMITS}", "--format=%h %ad %an: %s", "--date=short",
                        f"{tree['sha']}..{head}", "--", path, check=False)
    rows = result.stdout.strip().splitlines()
    if not rows:
        return [f"В ветке {branch} файл {path} после ревизии {tree['sha'][:8]} не менялся."]
    return [f"В ветке {branch} файл {path} менялся после ревизии {tree['sha'][:8]} — возможно, "
            "ошибку уже исправили (новые сверху):", *rows,
            "Если коммит по сообщению — про эту ошибку, назови его в разделе ИСТОРИЯ разбора."]


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(compare))
