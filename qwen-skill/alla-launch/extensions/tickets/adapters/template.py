"""Начальная точка пользовательского адаптера tickets (JSON stdin/stdout, api = 1).

Скопируйте файл в alla-extensions/_adapters/ и реализуйте пять функций ниже.
Подключайте явно через adapter_command; этот шаблон ничего не создаёт и не ищет.
Учётные данные читайте только из разрешённых adapter_env; не включайте их в ответы.
Полный контракт и примеры — ../README.md встроенного расширения tickets.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def prepare(request: dict[str, Any]) -> dict[str, Any]:
    """Чистое детерминированное преобразование: scope, capabilities, payload base64.

    Scope — сайт/экземпляр + проект/очередь; не сеанс, время или request_id.
    Здесь разрешены чтения справочников, но запрещены создание/резервирование объектов.
    Не меняйте содержание фактических разделов ticket. Всё, что будет отправлено,
    сериализуйте один раз в payload, который create/link затем отправят без изменения.
    """
    raise NotImplementedError


def search(request: dict[str, Any]) -> dict[str, Any]:
    """Вернуть matches с id, url, state, reason. Сбой — ошибка, не пустой список.

    Если поиск не реализован, prepare объявляет capabilities.search = false.
    Тогда расширение разрешит только create с явной пометкой в просмотре.
    """
    raise NotImplementedError


def create(request: dict[str, Any]) -> dict[str, Any]:
    """Отправить сохранённые байты payload; request_id — ключ идемпотентности.

    Не вызывайте prepare повторно и не добавляйте новые тексты/поля.
    Вернуть outcome = found и ticket {id, url, state} только после подтверждения трекера.
    """
    raise NotImplementedError


def link(request: dict[str, Any]) -> dict[str, Any]:
    """Добавить связь и доказательства в существующий тикет из frozen payload.

    Это внешнее изменение: локальная запись связи не считается успехом.
    Для идемпотентности используйте request_id, как в create.
    """
    raise NotImplementedError


def lookup(request: dict[str, Any]) -> dict[str, Any]:
    """Вернуть outcome = found | absent | unknown для исходного request_id.

    Absent допустим только при гарантии, что запрос не выполнен и уже не завершится
    позднее. Пустой поиск/задержка индексации — unknown. Возможность этой гарантии
    отдельно объявляется prepare: capabilities.authoritative_absence = true.
    """
    raise NotImplementedError


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(request, dict) or type(request.get("api")) is not int or request["api"] != 1:
            raise ValueError
        operations = {"prepare": prepare, "search": search, "create": create,
                      "link": link, "lookup": lookup}
        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in operations:
            raise ValueError
        response = operations[operation](request)
        print(json.dumps(response, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:  # граница частного API: не переносим его исключения и секреты в stdout
        print(json.dumps({"api": 1, "error": "Адаптер трекера ещё не подключён"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
