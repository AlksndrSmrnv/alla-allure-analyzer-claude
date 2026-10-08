"""Отложенный корпус (holdout): только проверка, по нему ничего не подбирается.

Правило работы — README эталона («Корпус: dev и holdout»). Сценарии holdout v1 перенесены в
``corpus_dev.py``: их тексты уже повлияли на решения. Новый набор собирается отдельным
коммитом.
"""

from __future__ import annotations

from collections.abc import Callable

from eval.corpus import Case

CASES: dict[str, Callable[[], Case]] = {}
