"""Отпечатки holdout для журнала просмотров (``holdout_journal.md``, правило — README).

    python tests/eval/journal.py    # текущие отпечатки корпуса и базовой линии holdout

Печатает только хэши: метрик и текстов сценариев не показывает, поэтому сам запуск — не
просмотр. ``test_eval_corpus.py`` сверяет их с последней записью журнала.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # запуск файлом: tests/ и scripts/ в sys.path
    _TESTS = Path(__file__).resolve().parents[1]
    for _path in (_TESTS, _TESTS.parent / "scripts"):
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

from eval import corpus_holdout  # noqa: E402
from eval.corpus import Case  # noqa: E402

JOURNAL = Path(__file__).resolve().parent / "holdout_journal.md"
BASELINE = Path(__file__).resolve().parent / "baseline.json"
_DIGEST = 16
_ENTRY_RE = re.compile(r"^- (корпус|базовая линия): `([0-9a-f]+)`", re.MULTILINE)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST]


def corpus_digest(cases: dict[str, Callable[[], Case]] | None = None) -> str:
    """Отпечаток сценариев: прогоны и разметка (генераторы детерминированы)."""
    cases = corpus_holdout.CASES if cases is None else cases
    dump = {name: asdict(factory()) for name, factory in sorted(cases.items())}
    return _sha(json.dumps(dump, default=bytes.hex, ensure_ascii=False, sort_keys=True))


def baseline_digest(baseline: dict[str, Any] | None = None) -> str:
    """Отпечаток раздела ``holdout`` базовой линии (нет раздела — пустой)."""
    data = json.loads(BASELINE.read_text(encoding="utf-8")) if baseline is None else baseline
    return _sha(json.dumps(data.get("holdout", {}), ensure_ascii=False, sort_keys=True))


def last_entry_digests(text: str | None = None) -> dict[str, str]:
    """Отпечатки последней записи журнала: ``{"корпус": …, "базовая линия": …}``."""
    if text is None:
        text = JOURNAL.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for kind, digest in _ENTRY_RE.findall(text):
        found[kind] = digest  # записи дописываются в конец: побеждает последняя
    return found


def main() -> int:
    print(f"- корпус: `{corpus_digest()}`")
    print(f"- базовая линия: `{baseline_digest()}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
