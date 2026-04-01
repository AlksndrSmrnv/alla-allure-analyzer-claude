Ты проектируешь новую интеграцию или контракт для текущего проекта alla.

По описанию пользователя выдавай только релевантные блоки и только в таком порядке:

## Блок 1: Контракт / Protocol

Куда добавлять:
- `src/alla/clients/base.py` — async HTTP/TestOps/LLM-подобные зависимости
- `src/alla/knowledge/base.py` — read-only KB search
- `src/alla/knowledge/feedback_store.py` — feedback, create/update KB entries, write-side storage
- отдельный protocol-модуль — только если ответственность не ложится в существующие файлы

Правила:
- Следуй стилю текущего слоя: для `clients/` методы обычно `async def`; для `knowledge/` store-контракты могут быть sync
- Возвращай реальные доменные модели или типизированные примитивы, которые нужны вызывающему коду
- Интерфейс должен быть минимальным, без «на всякий случай»
- Короткий docstring: зачем контракт существует

Пример async Protocol:

    class NewProvider(Protocol):
        """Read-only access to <source name>."""
        async def get_item(self, item_id: int) -> SomeModel: ...

Пример sync store Protocol:

    class NewStore(Protocol):
        """Persistent storage for <feature name>."""
        def save(self, payload: SomeModel) -> bool: ...

## Блок 2: Модели и конфиг

Указывай:
- куда добавить модели: `src/alla/models/` или `src/alla/knowledge/*_models.py`
- какие поля optional и почему
- нужен ли `ConfigDict(populate_by_name=True, extra="allow")` для внешнего API
- какие настройки добавить в `src/alla/config.py`

Важно:
- В проекте используется `env_prefix="ALLURE_"`, поэтому имя env var обычно выводится из имени поля автоматически
- `alias` добавляй только если без него реально нельзя; это не основной паттерн текущего `config.py`
- Если конфиг user-facing, обновляй также `.env.example` и `CLAUDE.md`

## Блок 3: Реализация

Куда создавать:
- `src/alla/clients/<name>_client.py` для внешнего HTTP/API
- `src/alla/knowledge/<name>_store.py`, `<name>_kb.py` или аналогичный storage-файл
- `src/alla/services/<name>_service.py` для бизнес-логики

Правила:
- `__init__` принимает только нужные зависимости: `Settings`, auth/http client, DSN, store, matcher и т.д.
- Сетевой код держи в `clients/`, а не в сервисах и не в entrypoint-слое
- Для skeleton-реализаций допустимы `# TODO: implement` и `raise NotImplementedError`
- Ошибки оборачивай в специализированное исключение из `alla/exceptions.py`

## Блок 4: Wiring

Указывай точное место подключения:
- Основной analysis pipeline → `src/alla/orchestrator.py`
- CLI/HTTP helper flow → `src/alla/cli.py`, `src/alla/server.py`, `src/alla/app_support.py`
- HTML/report behaviour → `src/alla/report/html_report.py`

Всегда уточняй:
- это часть `analyze_launch()` или отдельный auxiliary endpoint
- какие условия включения использовать (`settings.kb_active`, `settings.llm_active`, `settings.push_to_testops`, новый флаг или непустой DSN/URL)

## Блок 5: Тесты

Указывай:
- какие тест-файлы обновить в `tests/`
- happy path
- disabled / not configured path
- error mapping
- backward compatibility для CLI/HTTP JSON/HTML, если контракт меняется

---

Если пользователь не уточнил критичные детали, сначала проясни:
- это read-only или read+write
- это analysis pipeline или вспомогательный HTTP/API flow
- нужна ли конфигурация через `ALLURE_*`
