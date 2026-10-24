"""Тесты, похожие на нестабильные (флаки): кого предложить перезапустить локально.

Признаки — по тесту, детерминированно:

* сильные — повтор в TestOps прошёл или упал с другой ошибкой (``retries.retry_group``);
* средний — сообщение похоже на временный сбой (таймаут, обрыв соединения, 502/503/504,
  «stale element»), если разбор не «тест»/«данные» и повторы не «все упали одинаково».

«Окружение» в разборе само по себе не признак: стенд лежал в CI и поднялся — тест пройдёт,
но он не нестабильный. Не предлагаются проблемы с подтверждённым выводом расширения и с
применённой правкой агента (их перезапуск — проверка правки) и тесты, у которых все
повторы в TestOps упали одинаково.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from alla_skill_lib.rerun import failed_tests_of
from alla_skill_lib.retries import retry_group

# Текст сообщения, а не класс исключения: в сообщении Allure класса обычно нет.
TRANSIENT_RE = re.compile(
    r"""
      expected\ condition\ failed:\ waiting\ for     # Selenium WebDriverWait
    | tried\ for\ \d+\ second
    | read\ timed\ out | connect(?:ion)?\ timed\ out | timed\ out\ after | timeout\ exceeded
    | sockettimeoutexception | (?:read|connect)timeout
    | connection\ reset | broken\ pipe
    | stale\ element\ reference | element\ click\ intercepted
    | gateway\ time-?out | service\ unavailable | bad\ gateway
    | (?:status(?:\ code)?[:=\ ]\s*|but\ was:?\ <?|http/\d(?:\.\d)?\ )50[234]\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
STRONG = frozenset({"retry_passed", "retry_different"})
# Категории, при которых временный сбой в сообщении — не довод: ошибка теста или данных.
STABLE_CATEGORIES = frozenset({"тест", "данные"})


def transient(message: str) -> bool:
    return bool(TRANSIENT_RE.search(message))


def signals_of(test: Mapping[str, Any], category: str | None) -> list[str]:
    """Признаки нестабильности теста (коды ``rerun.SIGNALS``), сильные первыми."""
    group = retry_group(test)
    signals = []
    if group == "passed":
        signals.append("retry_passed")
    elif group == "different":
        signals.append("retry_different")
    if (category not in STABLE_CATEGORIES and group != "same"
            and transient(str(test.get("status_message") or ""))):
        signals.append("transient")
    return signals


def candidates(
    run: dict[str, Any],
    categories: Mapping[str, str | None],
    excluded: Iterable[str],
) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    """(запись кластера, тест, признаки): сначала с сильными признаками, затем крупные проблемы."""
    skip = set(excluded)
    found: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    for entry in run["clusters"]:
        if entry["file_id"] in skip:
            continue
        for test in failed_tests_of(run, entry):
            signals = signals_of(test, categories.get(entry["file_id"]))
            if signals:
                found.append((entry, test, signals))
    found.sort(key=lambda item: (not STRONG.intersection(item[2]), -int(item[0].get("member_count") or 0)))
    return found
