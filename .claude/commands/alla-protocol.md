Ты генерируешь скелет новой интеграции для проекта alla.

По описанию пользователя выдавай четыре блока последовательно:

## Блок 1: Protocol

Куда добавить: `src/alla/clients/base.py` (для HTTP-клиентов) или `src/alla/knowledge/base.py` (для KB).

Правила:
- Класс наследует `Protocol` из `typing`
- Все методы `async def`, возвращают конкретные доменные модели из `alla/models/`
- Только то, что реально нужно — минимальный интерфейс
- Короткий docstring: зачем Protocol существует

Пример:

    class NewProvider(Protocol):
        """Read-only access to <source name>."""
        async def get_item(self, id: int) -> SomeModel: ...
        async def get_items(self, parent_id: int) -> list[SomeModel]: ...

## Блок 2: Stub-реализация

Куда создать: `src/alla/clients/<name>_client.py` или `src/alla/knowledge/<name>_kb.py`.

Правила:
- `__init__` принимает `settings: Settings` + нужные зависимости (httpx.AsyncClient, AllureAuthManager и т.д.)
- Все методы Protocol реализованы с телом-заглушкой и `# TODO: implement`
- Ошибки оборачивать в специфическое исключение из `alla/exceptions.py`

Пример:

    class NewClient:
        def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
            self._settings = settings
            self._client = http_client

        async def get_item(self, id: int) -> SomeModel:
            # TODO: implement — GET /api/item/{id}
            raise NotImplementedError

## Блок 3: Изменение в orchestrator.py

Указывай:
- В каком месте `analyze_launch()` создать экземпляр
- Как передать в нужный сервис
- Условие включения (если opt-in: `if settings.new_feature_enabled:`)

## Блок 4: Env var в config.py

Указывай:
- Имя поля и тип: `new_feature_enabled: bool = False`
- Имя env var через `alias`: `ALLURE_NEW_FEATURE_ENABLED`
- Комментарий: что включает

---

Если пользователь не уточнил детали — спроси: read-only или read+write, нужна ли пагинация, есть ли аутентификация.
