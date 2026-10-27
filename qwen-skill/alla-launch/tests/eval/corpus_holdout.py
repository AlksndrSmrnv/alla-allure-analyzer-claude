"""Отложенный корпус (holdout): только проверка, по нему ничего не подбирается.

Правило работы — README эталона («Правило работы с holdout»). Holdout v3 перенесён в dev
2026-10-27; новый пишется по ``holdout_brief.md``.
"""

from __future__ import annotations

from collections.abc import Callable

from eval.corpus import Case

CASES: dict[str, Callable[[], Case]] = {}
