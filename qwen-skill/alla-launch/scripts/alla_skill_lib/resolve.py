"""Статус passed в TestOps для тестов, прошедших при перезапуске (``rerun --remember``).

Единственная запись скилла в TestOps. После «да» пользователя на вопрос ответа
``rerun_done`` каждый прошедший тест — и нестабильный, и «проверка правки» (решение
пользователя, 2026-10-26) — получает ``POST /api/testresult/{id}/resolve`` со статусом
passed. Запрос идёт на каждый результат отдельно, включая другие варианты (параметры)
метода: метод прошёл, только если прошли все его вызовы.

Отмеченные результаты записываются в ``rerun/resolved.json`` задания, поэтому повтор той же
команды отправляет только то, что не прошло в прошлый раз. Выключает отметку
``ALLURE_RERUN_RESOLVE=false``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alla_core.config import Settings
from alla_core.exceptions import AllaError, AllureApiError, AuthenticationError
from alla_skill_lib.errors import fetch_error_hint
from alla_skill_lib.rerun import first_line, safe_text

STATUS = "passed"
# Потолок одного запроса (с обменом токена и его повторами) после срока ответа: Qwen
# обрывает команду через 120 с, срок — ``rerun.WAIT_BUDGET`` (75 с) от её начала.
HARD_MARGIN_S = 30.0
NO_ANSWER = ("TestOps не ответил за время одного ответа команды — изменился ли статус, "
             "неизвестно; повтор той же команды отправит запрос снова (повторная отметка "
             "безвредна)")
# Ошибки, которые повторятся на каждом результате: дальше запросы не шлём.
_SHARED_STATUSES = frozenset({0, 401, 403})


def _shared(exc: BaseException) -> bool:
    if isinstance(exc, AuthenticationError):
        return True
    return isinstance(exc, AllureApiError) and (
        exc.status_code in _SHARED_STATUSES or 300 <= exc.status_code < 400)


@dataclass(frozen=True)
class Mark:
    """Прошедший тест плана: какие результаты TestOps отметить и с какой пометкой."""

    ids: list[int]  # результат теста плана и других его вариантов, ещё не отмеченные
    full_name: str
    file_id: str
    changed: str  # прошёл на изменённом коде теста — «проверка правки»
    notes: dict[int, str]  # ``message`` для TestOps по id: у каждого варианта своя ошибка CI


def plan_marks(
    result: Mapping[str, Any], plan: Mapping[str, Any], resolved: Mapping[int, str],
    messages: Mapping[int, str] | None = None,
) -> list[Mark]:
    """Прошедшие тесты итога, у которых в TestOps ещё есть не отмеченные результаты.

    ``messages`` — ошибка CI каждого результата (``run.json``); нет — ошибка теста плана.
    """
    planned = {int(test["test_result_id"]): test for test in plan.get("tests") or []}
    attempts = int(result.get("attempts") or 0)
    marks: list[Mark] = []
    for item in result.get("tests") or []:
        if item.get("outcome") != "passed":
            continue
        test_id = int(item["test_result_id"])
        test = planned.get(test_id, {})
        ids = [value for value in [test_id, *(int(other) for other in test.get("other_ids") or [])]
               if value not in resolved]
        if ids:
            own = str(test.get("message") or "")
            notes = {value: note(item, attempts, (messages or {}).get(value) or own)
                     for value in ids}
            marks.append(Mark(ids, str(item.get("full_name") or test_id),
                              str(item.get("file_id") or ""), str(item.get("changed") or ""),
                              notes))
    return marks


def note(item: Mapping[str, Any], attempts: int, ci_message: str) -> str:
    """Пометка для TestOps: прошёл при перезапуске, на каком запуске, и ошибка CI."""
    text = (f"alla: тест прошёл при перезапуске на машине инженера (запуск {item.get('attempt')} "
            f"из {attempts}).")
    if item.get("changed"):
        text += f" Прошёл на изменённом коде теста ({item['changed']}); в CI этого изменения ещё нет."
    ci = safe_text(first_line(ci_message))
    return text + (f" Ошибка в CI: {ci}" if ci else "")


def names(marks: Iterable[Mark]) -> str:
    return "; ".join(mark.full_name for mark in marks)


@dataclass
class Outcome:
    """Итог отметки: отмеченные результаты, не отмеченные тесты с причиной и неотправленные.

    ``left`` — не успели за один ответ команды (``deadline``): их отправит повтор команды.
    """

    done: dict[int, str]
    failed: list[tuple[Mark, str]]
    left: list[Mark] = field(default_factory=list)

    def done_marks(self, marks: Iterable[Mark]) -> list[Mark]:
        """Тесты, у которых отмечены все результаты."""
        return [mark for mark in marks if all(value in self.done for value in mark.ids)]

    def partly(self, marks: Iterable[Mark]) -> list[tuple[Mark, int]]:
        """Тесты с вариантами, у которых отмечена только часть результатов, и сколько."""
        counts = [(mark, sum(1 for value in mark.ids if value in self.done)) for mark in marks]
        return [(mark, done) for mark, done in counts if 0 < done < len(mark.ids)]


async def send(
    settings: Settings, marks: list[Mark], env_file: Path, today: str, *,
    deadline: float, on_done: Callable[[int], None],
) -> Outcome:
    """Отметить результаты по одному; ошибка доступа или сети останавливает остальные.

    Qwen обрывает команду через 120 с: после ``deadline`` (``time.monotonic``) новые запросы
    не начинаются, кроме первого — повтор команды всегда продвигается; каждый запрос вместе с
    обменом токена ограничен ``deadline + HARD_MARGIN_S``. ``on_done`` получает каждый
    принятый id сразу: оборванная команда не теряет уже отмеченное.
    """
    from alla_core.clients.auth import AllureAuthManager
    from alla_core.clients.testops_client import AllureTestOpsClient

    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
        retries=settings.http_retries,
    )
    outcome = Outcome({}, [])
    shared = ""
    sent = False
    async with AllureTestOpsClient(settings, auth) as client:
        for mark in marks:
            reason = shared
            unsent = False
            for test_id in mark.ids:
                if shared:
                    break
                if sent and time.monotonic() >= deadline:
                    unsent = True
                    break
                sent = True
                limit = max(deadline + HARD_MARGIN_S - time.monotonic(), 1.0)
                try:
                    row = await asyncio.wait_for(
                        client.resolve_test_result(test_id, STATUS, mark.notes[test_id]), limit)
                except TimeoutError:
                    reason = shared = reason or NO_ANSWER
                    continue
                except AllaError as exc:
                    reason = reason or failure(exc, settings, env_file)
                    if _shared(exc):
                        shared = reason
                    continue
                # Ответ 2xx без статуса (пустое тело) — принят: сверить нечем.
                status = str(row.get("status") or STATUS).lower()
                if status != STATUS:
                    reason = reason or f"TestOps оставил статус {safe_text(status, 40)}"
                    continue
                outcome.done[test_id] = today
                on_done(test_id)
            if reason:
                outcome.failed.append((mark, reason))
            elif unsent:
                outcome.left.append(mark)
    return outcome


def failure(exc: BaseException, settings: Settings, env_file: Path) -> str:
    """Почему результат не отмечен — словами, с действием для пользователя."""
    if isinstance(exc, AllureApiError) and exc.status_code == 403:
        return ("у токена нет права менять результаты тестов в проекте TestOps: нужен токен "
                "пользователя с правом записи (или выключите отметку: ALLURE_RERUN_RESOLVE=false "
                f"в {env_file})")
    if isinstance(exc, AllureApiError) and 300 <= exc.status_code < 400:
        return (f"TestOps ответил перенаправлением (HTTP {exc.status_code}): запрос перехватил "
                "прокси или страница входа, статус не изменён — проверьте ALLURE_ENDPOINT "
                f"в {env_file}")
    if isinstance(exc, AllureApiError) and exc.status_code == 404:
        return ("TestOps не нашёл результат теста или не знает запрос resolve (сверьте версию "
                "TestOps)")
    hint = fetch_error_hint(exc, settings, env_file)
    if hint:
        return hint
    return f"TestOps ответил ошибкой: {safe_text(str(exc)) or 'текст ошибки скрыт'}"
