"""Построитель синтетических прогонов с разметкой для эталона.

Прогон и разметка (``labels.json``, формат — в ``README.md``) строятся одновременно, без
случайности: один и тот же генератор всегда даёт одни и те же байты.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from skill_fake_testops import LaunchFixture

ACTIVE_STATUSES = frozenset({"failed", "broken"})
CATEGORIES = frozenset({"тест", "приложение", "окружение", "данные", "неизвестно"})


@dataclass
class Case:
    """Один размеченный прогон корпуса."""

    name: str
    fixture: LaunchFixture
    labels: dict[str, Any]
    heavy: bool = False  # большой прогон: только по явному запросу (run_eval.py --heavy)


@dataclass
class _Group:
    id: str
    cause: str | None
    category: str
    tests: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


class LaunchBuilder:
    """Собирает ``LaunchFixture`` и разметку групп симптомов."""

    def __init__(self, launch_id: int, name: str, *, first_result_id: int = 1000) -> None:
        self._launch = {"id": launch_id, "name": name, "projectId": 5}
        self._next_result = first_result_id
        self._next_attachment = launch_id * 100_000
        self._fixture = LaunchFixture(launch=self._launch, results=[])
        self._groups: dict[str, _Group] = {}
        self._retry_links: list[dict[str, Any]] = []
        self._passed_after_retry: list[int] = []

    def _result_id(self) -> int:
        self._next_result += 1
        return self._next_result

    def add_failure(
        self,
        group: str,
        *,
        name: str,
        cause: str | None,
        category: str = "неизвестно",
        full_name: str | None = None,
        message: str | None = None,
        trace: str | None = None,
        step: str | None = None,
        log: str | None = None,
        log_name: str = "app.log",
        evidence: Iterable[str] = (),
        status: str = "failed",
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Активное падение группы ``group``; первая запись задаёт ``cause`` и ``category``."""
        if status not in ACTIVE_STATUSES:
            raise ValueError(f"активное падение со статусом {status!r}")
        if category not in CATEGORIES:
            raise ValueError(f"неизвестная категория {category!r}")
        known = self._groups.setdefault(group, _Group(group, cause, category))
        if (known.cause, known.category) != (cause, category):
            raise ValueError(f"группа {group}: другая причина или категория")
        result_id = self.add_result(
            name=name, status=status, full_name=full_name, message=message, trace=trace,
            step=step, log=log, log_name=log_name, extra=extra,
        )
        known.tests.append(result_id)
        for line in evidence:
            if line not in known.evidence:
                known.evidence.append(line)
        return result_id

    def add_result(
        self,
        *,
        name: str,
        status: str,
        full_name: str | None = None,
        message: str | None = None,
        trace: str | None = None,
        step: str | None = None,
        log: str | None = None,
        log_name: str = "app.log",
        hidden: bool = False,
        muted: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Любой результат прогона; активные падения добавляйте через :meth:`add_failure`."""
        result_id = self._result_id()
        result: dict[str, Any] = {"id": result_id, "name": name, "status": status}
        if full_name:
            result["fullName"] = full_name
        details = {key: value for key, value in (("message", message), ("trace", trace)) if value}
        if details:
            result["statusDetails"] = details
        if hidden:
            result["hidden"] = True
        if muted:
            result["muted"] = True
        result.update(extra or {})
        self._fixture.results.append(result)
        if step:
            step_data: dict[str, Any] = {"name": step, "status": status}
            if details:
                step_data["statusDetails"] = dict(details)
            self._fixture.executions[result_id] = [step_data]
        if log is not None:
            self._next_attachment += 1
            self._fixture.attachments[result_id] = [
                {"id": self._next_attachment, "name": log_name, "type": "text/plain"}
            ]
            self._fixture.contents[self._next_attachment] = log.encode("utf-8")
        return result_id

    def add_detail(self, result_id: int, *, message: str | None = None,
                   trace: str | None = None) -> None:
        """Ответ ``GET /api/testresult/{id}`` — например, для попытки без ``statusDetails``."""
        details = {key: value for key, value in (("message", message), ("trace", trace)) if value}
        self._fixture.details[result_id] = {"id": result_id, "statusDetails": details}

    def expect_attempts(self, final: int, attempts: Iterable[tuple[int, bool | None]]) -> None:
        """Разметка повторов: попытки финального результата по порядку и «та же ошибка»."""
        self._retry_links.append({"final": final, "attempts": [
            {"id": attempt, "same": same} for attempt, same in attempts]})

    def expect_passed_after_retry(self, final: int) -> None:
        self._passed_after_retry.append(final)

    def labels(self) -> dict[str, Any]:
        labels: dict[str, Any] = {"groups": [
            {"id": group.id, "cause": group.cause, "category": group.category,
             "tests": list(group.tests), "evidence": list(group.evidence)}
            for group in self._groups.values()
        ]}
        if self._retry_links or self._passed_after_retry:
            labels["retries"] = {"links": list(self._retry_links),
                                 "passed_after_retry": list(self._passed_after_retry)}
        return labels

    def build(self, name: str, *, heavy: bool = False) -> Case:
        case = Case(name, self._fixture, self.labels(), heavy)
        validate_labels(case.fixture, case.labels)
        return case


def active_failure_ids(fixture: LaunchFixture) -> list[int]:
    """Результаты, которые триаж считает активными падениями."""
    return [
        int(result["id"]) for result in fixture.results
        if str(result.get("status", "")).lower() in ACTIVE_STATUSES
        and not result.get("hidden") and not result.get("muted")
    ]


def validate_labels(fixture: LaunchFixture, labels: dict[str, Any]) -> None:
    """Каждое активное падение — ровно в одной группе, лишних тестов в разметке нет."""
    seen: dict[int, str] = {}
    group_ids: set[str] = set()
    for group in labels["groups"]:
        if not group.get("id"):
            raise ValueError("группа без id")
        if group["id"] in group_ids:
            raise ValueError(f"повторяется id группы {group['id']}")
        group_ids.add(group["id"])
        for test_id in group["tests"]:
            if test_id in seen:
                raise ValueError(f"тест {test_id} в группах {seen[test_id]} и {group['id']}")
            seen[test_id] = group["id"]
    active = set(active_failure_ids(fixture))
    if missing := sorted(active - set(seen)):
        raise ValueError(f"активные падения без группы: {missing}")
    if extra := sorted(set(seen) - active):
        raise ValueError(f"в разметке не активные падения: {extra}")
    retries = labels.get("retries")
    if retries:
        hidden = {int(result["id"]) for result in fixture.results if result.get("hidden")}
        for link in retries.get("links", []):
            if link["final"] not in active:
                raise ValueError(f"повторы: {link['final']} — не активное падение")
            if strange := sorted({a["id"] for a in link["attempts"]} - hidden):
                raise ValueError(f"повторы: {strange} — не hidden-результаты")
        finals = {int(result["id"]) for result in fixture.results if not result.get("hidden")}
        if strange := sorted(set(retries.get("passed_after_retry", [])) - finals):
            raise ValueError(f"прошли после повтора: {strange} — нет таких финальных результатов")


def java_trace(exception: str, frames: Iterable[str]) -> str:
    """Стек Java: строка исключения и кадры ``\\tat …``."""
    return "\n".join([exception, *(f"\tat {frame}" for frame in frames)]) + "\n"


def noise_lines(prefix: Callable[[int], str], count: int, text: Callable[[int], str]) -> str:
    """``count`` обычных строк лога: ``prefix(i) + text(i)``."""
    return "".join(f"{prefix(index)}{text(index)}\n" for index in range(count))
