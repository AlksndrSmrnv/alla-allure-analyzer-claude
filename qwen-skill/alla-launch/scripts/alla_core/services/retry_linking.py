"""Связь попыток (``hidden``-результатов) с финальными результатами того же выполнения.

По документации TestOps попытки одного выполнения связаны «идентичностью контекста
выполнения» (``historyId``): тест + параметры + окружение; при смене параметров или
окружения повтор становится отдельным результатом. Настоящих ответов с повторами нет,
поэтому ключ выбирается по тому, какие поля есть в ответе (модели принимают
неизвестные поля, ``extra="allow"``):

1. ``historyId``, затем ``historyKey``;
2. ``testCaseId`` + нормализованные ``parameters`` + ``environment`` — только если есть
   ``testCaseId`` и хотя бы одно из двух полей: один ``testCaseId`` смешал бы
   параметризованные тесты;
3. иначе попытки не связываются, и это видно в :class:`RetryInfo`.

Чистые функции без сети; ошибки попыток догружает ``TriageService``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from alla_core.models.testops import RetryInfo, TestResultResponse

HISTORY_FIELDS = ("historyId", "historyKey")
CONTEXT_KEY = "testCaseId+parameters+environment"
_CONTEXT_FIELDS = ("parameters", "environment")


@dataclass
class RetryLinks:
    """Попытки каждого финального результата по порядку и счётчики связывания."""

    linked_by: str | None = None
    attempts: dict[int, list[TestResultResponse]] = field(default_factory=dict)
    hidden_total: int = 0
    linked: int = 0
    no_key: int = 0
    no_final: int = 0
    ambiguous: int = 0

    def info(self) -> RetryInfo:
        return RetryInfo(
            linked_by=self.linked_by,
            hidden_total=self.hidden_total,
            linked=self.linked,
            no_key=self.no_key,
            no_final=self.no_final,
            ambiguous=self.ambiguous,
        )


def link_attempts(results: list[TestResultResponse]) -> RetryLinks:
    """Связать скрытые попытки с финальными (не ``hidden``) результатами по общему ключу.

    Ключ выбирается один на прогон (:func:`link_field`). Если финальных результатов с
    одним ключом несколько, их попытки не связываются: угадывать не будем.
    """
    hidden = [result for result in results if result.hidden]
    links = RetryLinks(linked_by=link_field(hidden), hidden_total=len(hidden))
    if not hidden:
        return links
    if links.linked_by is None:
        links.no_key = len(hidden)
        return links

    finals: dict[str, list[int]] = defaultdict(list)
    for result in results:
        if not result.hidden:
            key = link_key(result, links.linked_by)
            if key is not None:
                finals[key].append(result.id)
    grouped: dict[int, list[TestResultResponse]] = defaultdict(list)
    for attempt in hidden:
        key = link_key(attempt, links.linked_by)
        owners = finals.get(key, []) if key is not None else []
        if key is None:
            links.no_key += 1
        elif not owners:
            links.no_final += 1
        elif len(owners) > 1:
            links.ambiguous += 1
        else:
            grouped[owners[0]].append(attempt)
            links.linked += 1
    links.attempts = {
        final_id: sorted(attempts, key=_order) for final_id, attempts in grouped.items()
    }
    return links


def link_field(hidden: list[TestResultResponse]) -> str | None:
    """Поле связи по скрытым попыткам: первое из :data:`HISTORY_FIELDS`, у которого есть
    значение хотя бы у одной попытки, иначе :data:`CONTEXT_KEY`, иначе ``None``."""
    for name in HISTORY_FIELDS:
        if any(_text(_extra(result, name)) for result in hidden):
            return name
    if any(_context_key(result) is not None for result in hidden):
        return CONTEXT_KEY
    return None


def link_key(result: TestResultResponse, linked_by: str) -> str | None:
    """Значение ключа связи у результата; ``None`` — у результата его нет."""
    if linked_by == CONTEXT_KEY:
        return _context_key(result)
    return _text(_extra(result, linked_by))


def retry_warnings(info: RetryInfo) -> list[str]:
    """Предупреждения прогона о повторах: только когда что-то не связалось или не загрузилось."""
    warnings: list[str] = []
    if info.hidden_total and info.linked_by is None:
        warnings.append(
            f"Повторы не связаны с финальными результатами (скрытых попыток: "
            f"{info.hidden_total}): в ответах TestOps нет ни historyId/historyKey, ни "
            "testCaseId с параметрами или окружением."
        )
    elif unlinked := info.no_key + info.no_final + info.ambiguous:
        parts = [
            f"{label}: {count}" for label, count in (
                ("без значения ключа", info.no_key),
                ("без финального результата", info.no_final),
                ("ключ у нескольких результатов", info.ambiguous),
            ) if count
        ]
        warnings.append(
            f"Не удалось связать {unlinked} из {info.hidden_total} скрытых попыток "
            f"(связь по {info.linked_by}; {', '.join(parts)})."
        )
    if info.errors_known < info.errors_total:
        line = (
            f"Ошибки повторов известны для {info.errors_known} из {info.errors_total} "
            "неудачных попыток"
        )
        if info.errors_capped:
            line += (
                f"; {info.errors_capped} не запрошены из-за лимита "
                "ALLURE_RETRY_MAX_DETAIL_REQUESTS"
            )
        warnings.append(line + ".")
    return warnings


def _extra(result: TestResultResponse, name: str) -> Any:
    return (result.model_extra or {}).get(name)


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int)):
        text = str(value).strip()
        return text or None
    return None


def _context_key(result: TestResultResponse) -> str | None:
    """``testCaseId`` + параметры + окружение; без ``testCaseId`` или без обоих полей — ``None``."""
    extra = result.model_extra or {}
    if result.test_case_id is None or not any(name in extra for name in _CONTEXT_FIELDS):
        return None
    parts = [str(result.test_case_id)]
    parts += [json.dumps(_pairs(extra.get(name)), ensure_ascii=False) for name in _CONTEXT_FIELDS]
    return "\x1f".join(parts)


def _pairs(value: Any) -> list[list[str]]:
    """Параметры или окружение — отсортированные пары ``[имя, значение]``.

    Порядок в ответе не важен. Параметры с ``excluded: true`` пропускаются: Allure не
    включает их в ``historyId``.
    """
    if value is None:
        return []
    items: list[Any]
    if isinstance(value, dict):
        items = [{"name": key, "value": item} for key, item in value.items()]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    pairs: list[list[str]] = []
    for item in items:
        if isinstance(item, dict):
            if item.get("excluded") is True:
                continue
            name = item.get("name", item.get("key", ""))
            pairs.append([str(name), str(item.get("value", ""))])
        else:
            pairs.append(["", str(item)])
    return sorted(pairs)


def _order(result: TestResultResponse) -> tuple[bool, int, int]:
    """По времени создания; без него — по id (результаты без времени — после)."""
    created = result.created_date
    return (created is None, created or 0, result.id)
