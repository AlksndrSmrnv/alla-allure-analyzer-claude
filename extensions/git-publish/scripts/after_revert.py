"""Хук ``after_revert`` расширения git-publish: правку откатили — напомнить о ветке на сервере.

Ветку и merge request скрипт не трогает: удалять чужое на сервере — решение человека.
Правку не выгружали — ничего не печатает, и скилл ничего не добавляет к ответу.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    context = json.loads(Path(os.environ["ALLA_CONTEXT"]).read_text(encoding="utf-8"))
    problem = context.get("problem") or {}
    result_file = Path(context["out_dir"]) / f"after_apply-{problem.get('file_id')}.result.json"
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(result, dict) or not result.get("pushed"):
        return 0
    branch, remote = result.get("branch"), result.get("remote")
    print(
        f"Правка откатана только локально: ветка {branch} с ней осталась на {remote}. Если она "
        f"больше не нужна — закрой merge request или удали ветку: git push {remote} --delete {branch}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
