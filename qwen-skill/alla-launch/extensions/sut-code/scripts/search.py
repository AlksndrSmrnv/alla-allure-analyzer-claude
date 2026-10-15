"""Действие ``search`` расширения sut-code: текст в коде выбранных ревизий (``git grep -F``).

Поиск Qwen (``grep_search``, ``glob``) копию не видит — она исключена своим ``.gitignore``,
поэтому субагент ищет этим действием: ``… action sut-code search --context … -- <текст>``.
Регистр не учитывается, текст — как есть (не регулярное выражение).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sutlib
from sutlib import SutError

LIMIT = 30


def search(context: dict[str, Any], context_file: Path) -> list[str]:
    text = sutlib.action_args()
    if len(text) < 3:
        raise SutError("что искать? Текст — после «--», не короче 3 символов")
    state = sutlib.load_state(context_file)
    one = len(state["trees"]) == 1
    rows: list[str] = []
    for tree in state["trees"]:
        for path, line, code in sutlib.grep(Path(tree["tree"]), text, limit=LIMIT,
                                            ignore_case=True):
            where = path if one else f"{tree['repo']}:{path}"
            rows.append(f"- {where}:{line}: {code}")
    if not rows:
        return [f"«{text}» в коде выбранных ревизий не найден."]
    head = [f"«{text}» найден (первые {LIMIT}; файлы читай в папке ревизии из данных):"]
    return head + rows[:LIMIT]


if __name__ == "__main__":
    raise SystemExit(sutlib.run_main(search))
