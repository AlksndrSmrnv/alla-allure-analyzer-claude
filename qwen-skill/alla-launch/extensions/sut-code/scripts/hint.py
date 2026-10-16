"""Хук ``after_report`` расширения sut-code: подсказать, какие проблемы разобрать по коду.

Проблемы категорий «приложение» и «неизвестно», которые инженер ещё не подтвердил, — их
причину может показать код приложения. Настройка ``hint = false`` выключает подсказку.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CATEGORIES = ("приложение", "неизвестно")
SHOWN = 5


def main() -> int:
    context = json.loads(Path(os.environ["ALLA_CONTEXT"]).read_text(encoding="utf-8"))
    settings = context.get("settings") or {}
    if not settings.get("hint", True):
        return 0
    numbers = [problem["number"] for problem in context.get("problems", [])
               if problem.get("category") in CATEGORIES and not problem.get("confirmed")]
    if not numbers:
        return 0
    listed = ", ".join(str(number) for number in numbers[:SHOWN])
    if len(numbers) > SHOWN:
        listed += f" и ещё {len(numbers) - SHOWN}"
    first = numbers[0]
    word = "Проблема" if len(numbers) == 1 else "Проблемы"
    print(f"{word} {listed}: причину может показать код приложения — разобрать по коду: "
          f"/alla:code {first} (если стенд был не на основной ветке — с версией: "
          f"/alla:code {first} <ветка или тег>).")
    if not settings.get("repos"):
        print(f"Ссылку на git приложения назови в команде (/alla:code {first} <ссылка>) или "
              "один раз в alla-extensions/config.toml: [sut-code] repos = [\"<ссылка>\"].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
