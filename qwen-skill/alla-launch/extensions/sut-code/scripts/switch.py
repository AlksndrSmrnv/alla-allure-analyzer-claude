"""Действие ``switch`` расширения sut-code: перейти на другую ревизию и пересобрать данные.

``… action sut-code switch --context … -- <ветка|тег|коммит>`` или ``-- <репозиторий>=<ревизия>``.
Нужен, если по логам видно, что стенд был на другой версии.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import prepare
import sutlib
from sutlib import Repo, SutError


def switch(context: dict[str, Any], context_file: Path) -> list[str]:
    value = sutlib.action_args()
    if not value:
        raise SutError("на какую ревизию перейти? Ветка, тег или коммит — после «--»")
    state = sutlib.load_state(context_file, need_trees=False)
    repo_name, sep, ref = value.partition("=")
    if not sep or repo_name not in {item["repo"] for item in state["trees"]}:
        repo_name, ref = "", value
    tree = sutlib.tree_of(state, repo_name or None)
    repo = Repo(tree["repo"], tree["url"], Path(tree["git_dir"]).parent)
    allow_local = bool(context["settings"].get("allow_local"))
    deadline = time.monotonic() + sutlib.WAIT_BUDGET
    with sutlib.locked(repo):
        resolved = sutlib.resolve_ref(repo, ref.strip())
        if resolved is None:
            similar = ", ".join(sutlib.similar_refs(repo, ref)) or "нет"
            raise SutError(f"в {repo.name} нет ревизии «{ref}»; похожие: {similar}")
        sha, what = resolved
        path = sutlib.ensure_tree(repo, sha, deadline, allow_local)
        in_use = {Path(item["tree"]) for item in state["trees"]} | {path}
        sutlib.cleanup_trees(repo, int(context["settings"].get("keep_trees", 3)), in_use)
    tree.update(sha=sha, ref=ref.strip(), how=f"{what} — выбрана при разборе", tree=str(path),
                commit=sutlib.commit_line(repo, sha))
    state["trees"] = [tree if item["repo"] == tree["repo"] else item for item in state["trees"]]
    sutlib.save_state(context_file, state)
    problem = context.get("problem") or {}
    data = sutlib.data_path(context_file)
    data.write_text(prepare.render_data(problem, state["trees"], problem.get("sources") or [],
                                        state.get("notes", [])), encoding="utf-8")
    return [f"{repo.name}: теперь ревизия {ref.strip()} ({sha[:8]}, {what}).",
            f"Папка ревизии: {path}/",
            f"Данные пересобраны — прочитай заново: {data}"]


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(switch))
