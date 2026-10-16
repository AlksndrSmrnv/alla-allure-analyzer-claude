"""Действие ``files`` расширения sut-code: файлы ревизий, в пути которых есть текст.

Замена ``glob`` Qwen (он копию не видит): ``… action sut-code files --context … -- OrderService``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sutlib
from sutlib import SutError

LIMIT = 40


def files(context: dict[str, Any], context_file: Path) -> list[str]:
    text = sutlib.action_args().lower()
    if len(text) < 3:
        raise SutError("какие файлы искать? Часть пути — после «--», не короче 3 символов")
    state = sutlib.load_state(context_file)
    rows: list[str] = []
    for tree in state["trees"]:
        for name in sutlib.tracked_files(Path(tree["tree"])):
            if text in name.lower():
                rows.append(f"- {tree['tree']}/{name}")
    if not rows:
        return [f"Файлов с «{text}» в пути в выбранных ревизиях нет."]
    more = f" (показаны первые {LIMIT} из {len(rows)})" if len(rows) > LIMIT else ""
    return [f"Файлы с «{text}» в пути{more}:", *rows[:LIMIT]]


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(files))
