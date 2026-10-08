"""Повторы тестов (шаг 5) в задании кластера и в отчёте: факты о воспроизводимости.

Попытки связывает и загружает ядро (``FailedTestSummary.attempts``, ``RetryInfo``); здесь —
только подсчёт и текст. Повторы причину не устанавливают: задание говорит это модели,
а в отчёте они идут строкой фактов рядом с разбором.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

FAILURE_STATUSES = ("failed", "broken")
MAX_DIFFERENT = 2
DIFFERENT_CHARS = 200
MAX_REPORT_PASSED = 20

TASK_NOTE = (
    "Правило скилла (не данные TestOps): повторы показывают, воспроизводится ли сбой, но "
    "причину не устанавливают. Ни один факт этого раздела (одинаковые падения, прошедшая "
    "попытка, неизвестная ошибка попытки) не довод ни за одну категорию; причину и "
    "категорию определяй по сообщению, трейсу, логу и коду.{different} В СОГЛАСОВАННОСТЬ "
    "ошибки попыток не входят. Этот раздел не цитируй в НАБЛЮДЕНИЯХ: у него нет id куска."
)
# Ошибка попытки бывает похожа на «окружение» (ConnectionRefused), а правило категорий
# относится к ошибке теста и логу: без запрета модель выбрала бы «окружение» (ревью
# qwen-executor-review, E09).
TASK_NOTE_DIFFERENT = (
    " Ошибка попытки — не ошибка финального падения: по ней категорию не выбирай (правило "
    "про ConnectionRefused и таймауты — для сообщения теста и лога), в ПРИЧИНУ и КАК "
    "ИСПРАВИТЬ её не переноси; упомяни её не больше чем одной фразой в ЧТО СЛОМАЛОСЬ или "
    "не упоминай."
)


@dataclass
class RetryFacts:
    """Сколько тестов группы повторялись и как упали их попытки.

    Тест с попытками попадает ровно в одну группу: ``different`` — хоть одна неудачная
    попытка с другой ошибкой; ``passed`` — иначе хоть одна попытка прошла; ``unknown`` —
    иначе у неудачной попытки ошибка неизвестна; ``other`` — иначе есть попытка с другим
    статусом (skipped, unknown); ``same`` — все разобранные попытки упали с той же ошибкой.
    Ядро хранит только последние попытки теста, ``omitted`` — сколько более ранних не
    разобрано: тогда утверждения и число попыток — только о разобранных.
    """

    tests: int = 0
    retried: int = 0
    attempts: int = 0
    same: int = 0
    different: int = 0
    passed: int = 0
    unknown: int = 0
    other: int = 0
    omitted: int = 0
    same_partial: int = 0
    different_messages: list[str] = field(default_factory=list)
    other_statuses: list[str] = field(default_factory=list)

    def items(self) -> list[str]:
        """Пункты «у N тестов …» без маркеров и знаков в конце; у одного теста — без «у N»."""
        def who(count: int) -> str:
            return "" if self.tests == 1 else f"у {_tests(count)} "

        items: list[str] = []
        if self.same:
            which = "все разобранные попытки" if self.same_partial else "все попытки"
            items.append(f"{who(self.same)}{which} упали с той же ошибкой")
        if self.different:
            text = f"{who(self.different)}есть попытка с другой ошибкой"
            shown = self.different_messages[:MAX_DIFFERENT]
            if shown:
                text += ": " + ", ".join(f"«{message}»" for message in shown)
                rest = len(self.different_messages) - len(shown)
                if rest > 0:
                    text += f" и ещё {rest}"
            items.append(text)
        if self.passed:
            items.append(f"{who(self.passed)}одна из попыток прошла")
        if self.unknown:
            items.append(f"{who(self.unknown)}ошибка попыток неизвестна")
        if self.other:
            statuses = ", ".join(self.other_statuses)
            items.append(f"{who(self.other)}есть попытка со статусом {statuses}")
        return items

    def headline(self) -> str:
        count = f"неудачных попыток до финальной: {self.attempts}"
        if self.tests != 1:
            count += ", всего по группе"
        if self.omitted:
            count = f"разобранных {count}; более ранних попыток не разобрано: {self.omitted}"
        if self.tests == 1:
            return f"Тест запускался повторно ({count})"
        return f"Повторы были у {self.retried} из {_tests(self.tests)} ({count})"


def retry_facts(tests: Iterable[Mapping[str, Any]]) -> RetryFacts:
    """Подсчитать повторы по тестам: ``attempts`` (словари ``AttemptSummary``) и
    ``attempts_omitted`` из ``FailedTestSummary`` в run.json."""
    facts = RetryFacts()
    for test in tests:
        facts.tests += 1
        attempts: Sequence[Mapping[str, Any]] = test.get("attempts") or []
        if not attempts:
            continue
        omitted = int(test.get("attempts_omitted") or 0)
        facts.retried += 1
        facts.omitted += omitted
        failures = [a for a in attempts if str(a.get("status")) in FAILURE_STATUSES]
        facts.attempts += len(failures)
        differing = [a for a in failures if a.get("same_as_final") is False]
        if differing:
            facts.different += 1
            for attempt in differing:
                message = _clip(" ".join(str(attempt.get("message") or "").split()))
                if message and message not in facts.different_messages:
                    facts.different_messages.append(message)
        elif any(str(a.get("status")) == "passed" for a in attempts):
            facts.passed += 1
        elif any(a.get("same_as_final") is None for a in failures):
            facts.unknown += 1
        elif len(failures) < len(attempts):
            facts.other += 1
            for attempt in attempts:
                status = str(attempt.get("status") or "unknown")
                if status not in FAILURE_STATUSES and status not in facts.other_statuses:
                    facts.other_statuses.append(status)
        else:
            facts.same += 1
            if omitted:
                facts.same_partial += 1
    return facts


def render_task_section(facts: RetryFacts) -> list[str]:
    """Раздел задания кластера; пустой, если повторов не было ни у одного теста."""
    if not facts.retried:
        return []
    items = facts.items()
    lines = ["--- Повторы в TestOps ---", facts.headline() + ":"]
    lines += [f"- {item}{';' if index < len(items) else '.'}"
              for index, item in enumerate(items, start=1)]
    note = TASK_NOTE.format(different=TASK_NOTE_DIFFERENT if facts.different else "")
    return [*lines, "", note]


def report_line(facts: RetryFacts) -> str | None:
    """Одна строка для подробностей проблемы в report.md; ``None`` — повторов не было."""
    if not facts.retried:
        return None
    return f"{facts.headline()}: {'; '.join(facts.items())}."


def passed_after_retry(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Прошедшие после повтора из run.json."""
    return list(run["triage"]["retries"]["passed_after_retry"])


def _tests(count: int) -> str:
    """«1 теста», «2 тестов», «21 теста» — родительный падеж после «у» и «из»."""
    noun = "теста" if count % 10 == 1 and count % 100 != 11 else "тестов"
    return f"{count} {noun}"


def _clip(text: str) -> str:
    return text if len(text) <= DIFFERENT_CHARS else text[:DIFFERENT_CHARS - 1].rstrip() + "…"
