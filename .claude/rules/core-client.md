---
paths:
  - "qwen-skill/alla-launch/scripts/alla_core/clients/**"
  - "qwen-skill/alla-launch/scripts/alla_core/config.py"
  - "qwen-skill/alla-launch/scripts/alla_core/exceptions.py"
  - "qwen-skill/alla-launch/scripts/alla_core/models/testops.py"
  - "qwen-skill/alla-launch/tests/test_core_testops_client.py"
  - "qwen-skill/alla-launch/tests/test_core_mcp_client.py"
  - "qwen-skill/alla-launch/tests/test_core_auth.py"
---

# Ядро: клиент TestOps и настройки

## Клиент TestOps — MCP (`clients/mcp_client.py`)

REST API TestOps у пользователей закрыт (решение пользователя, 2026-11-02): читает скилл только
через встроенный MCP-сервер TestOps (`Settings.mcp_endpoint` — `ALLURE_MCP_URL` или
`<ALLURE_ENDPOINT>/api/mcp`, TestOps 26.2.1.8+). `TestOpsMcpClient` реализует
`TestResultsProvider` и `AttachmentProvider` (`clients/base.py`) — сервисы ядра его не отличают
от прежнего REST-клиента. Схемы инструментов Qameta не публикует; что известно — проверено
пользователем на настоящем TestOps:

- Один инструмент — `testops_find_testresults`. Список прогона — `aql: "launch = <id>"` со
  страницами, без expand. Полный результат — `aql: "id = <id>"` строго с **одним** id (со
  списком expand не работает) и `expand: ["scenario", "fixtures", "attachments"]`; ответ —
  результат с `scenario` (дерево шагов, вложения в `steps[]`), `fixtures` и словарём
  `attachments` `{"<id>": {..., "content", "contentLength"}}`: текст (`text/plain`,
  `text/html`) — строкой как есть, бинарное (`image/png`) — base64 в том же `content`,
  `contentLength` — размер исходника.
- Имена параметров (aql, страница, размер, expand) — из `inputSchema` (`_tool_params`,
  кандидаты `_PARAM_CANDIDATES`); схемы нет — `aql`/`page`/`size`/`expand`. Нет параметра
  страницы — одна страница. Страница ответа: `content`/`items`/`results`/`data` или список;
  итогов (`totalPages`/`totalElements`/`total`) нет — следующая страница, пока эта полная
  (`last_total_elements` тогда — сколько получено).
- Транспорт — свой Streamable HTTP на `httpx` (пакет `mcp` не берём: веб-сервер и десяток
  зависимостей в lock): JSON-RPC в POST, `Authorization: api-token <token>`,
  `Accept: application/json, text/event-stream`, `Mcp-Session-Id` из ответа `initialize`,
  `MCP-Protocol-Version` после него; ответ JSON или SSE (`rpc_reply` ищет сообщение со своим
  id). `initialize` → `notifications/initialized` → `tools/list` (с `nextCursor`) лениво, при
  первом вызове; `connect()` — то же явно (`check`). 404 при открытом сеансе — сервер его
  забыл: один новый сеанс и повтор. Ошибки — `McpError(reason)`: `not_found` (404 без сеанса),
  `transport` (405: старый SSE-транспорт не поддерживается), `no_tool`, `tool` (`isError`,
  JSON-RPC error), `protocol`; 401/403 и прочие HTTP — `AllureApiError` со статусом
  (`errors.fetch_error_hint` различает их).
- **Повторы при временных сбоях.** Все вызовы MCP читают, поэтому повторяются как GET прежнего
  клиента: до `http_retries` раз при `RETRY_STATUSES` (429, 502, 503, 504) и
  `httpx.RequestError`; пауза — `Retry-After` в секундах, иначе 1, 2, 4… с, не больше 30 с
  (`clients/retry.py`). 500 не повторяется. Последний сбой сети — `AllureApiError(0, …)`.
  Причина: один 502 балансировщика при пагинации обрывал весь `prepare`. Тесты подменяют
  `_sleep` клиента.
- **Один запрос на результат.** Полный ответ кешируется (`_full`, блокировка на id):
  `get_test_result_execution`, `get_test_result_detail`, `get_attachments_for_test_result` и
  `get_attachment_content` его переиспользуют. Шаги: `scenario` (`{steps}`, список,
  `{content}`, обёртка с ошибкой — сама корень), фикстуры before — в начало дерева, after
  (`type`/ключ с `after`, `teardown`) — в конец; вложения шага (id или объект) — метаданными
  из словаря, без `content`. `message`/`trace` верхнего уровня без `statusDetails` — в
  `statusDetails` (`_result`). Без expand (`aql: id = N`) — `get_test_result_detail` для
  ошибок скрытых попыток, если полного ответа нет.
- **Содержимое вложений** (`_decode`): текстовый MIME — строка как есть, другой MIME — base64;
  без MIME — base64, только если раскодированное похоже на файл (сигнатура PNG/JPEG/GIF/RIFF/
  PDF/ZIP или нулевые байты): «test» — правильный base64, а лог без типа должен остаться
  логом. Хранится не больше `max(logs_max_attachment_bytes, images_max_bytes + 1)` байт, в
  пределах `content_budget` (128 МБ, LRU); вытесненное запрашивается заново тем же полным
  запросом результата (`_owner`: вложение → результат). Неизвестное вложение — 404.
- `get_attachment_content(id, max_bytes=None)` — потолок по умолчанию
  `logs_max_attachment_bytes`; скриншоты скилл берёт со своим (`images_max_bytes` + 1 байт:
  длиннее потолка — обрезан, пропускается). Протокол `AttachmentProvider` параметра не
  требует: фейковые провайдеры логов его не знают.
- **Прогон** (`get_launch`): инструмента нет — из первой страницы результатов (кешируется для
  `get_all_test_results_for_launch`): имя — `launchName` или `launch.name`, проект —
  `projectId`, `closed` — `launch.closed`, если сервер их прислал; иначе `None`. Время прогона
  `None` — триаж берёт самый ранний `createdDate` результатов (`_earliest_created`). Пустой и
  несуществующий прогон не различить: `prepare` говорит «нет ни одного результата».
- API-модели принимают неизвестные поля (`extra="allow"`) и оба стиля имён
  (`populate_by_name=True`): payloads TestOps плавают.
- Не проверено на настоящем TestOps (данных нет): имена параметров страниц, форма страницы и
  `fixtures`, входят ли hidden-попытки в `launch = <id>`, транспорт сервера. Проверка —
  `check` и `prepare` у пользователя.

## REST-клиент — только resolve (`clients/testops_client.py`)

`AllureTestOpsClient` оставлен для единственной записи, которой в MCP нет, — статуса passed
(`skill-rerun-resolve.md`), по умолчанию выключенной. Обмен API-токена на JWT
(`AllureAuthManager`, `POST /api/uaa/oauth/token`, повторы как у чтения) и один повтор при 401
с новым JWT; resolve не повторяется (запись).

## Настройки (`config.py`)

`Settings` читает `<skill>/.env` и переменные окружения `ALLURE_*` (окружение важнее файла),
чужие ключи игнорирует. Границы значений — `BOUNDS` (ge/le). `load(require_testops=False)` — для
команд без TestOps (`rerun`): адрес и токен не обязательны, остальное проверяется.

| Переменная | Обязательная | По умолчанию | Описание |
|---|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps без `/api` |
| `ALLURE_TOKEN` | да | `""` | API token |
| `ALLURE_MCP_URL` | нет | `""` | Адрес MCP-сервера TestOps; пусто — `<ALLURE_ENDPOINT>/api/mcp`; иначе `http(s)://` |
| `ALLURE_REQUEST_TIMEOUT` | нет | `30` | HTTP timeout к TestOps |
| `ALLURE_HTTP_RETRIES` | нет | `3` | Повторы вызовов MCP при 429/502/503/504 и сбоях сети (0–10) |
| `ALLURE_PAGE_SIZE` | нет | `100` | Размер страницы TestOps API |
| `ALLURE_MAX_PAGES` | нет | `200` | Защита пагинации; больше страниц — `PaginationLimitError` сразу после первой страницы |
| `ALLURE_DETAIL_CONCURRENCY` | нет | `10` | Параллелизм запросов результатов (полный expand) |
| `ALLURE_LOGS_CONCURRENCY` | нет | `5` | Параллелизм скачивания attachments |
| `ALLURE_LOGS_MAX_ATTACHMENT_BYTES` | нет | `10485760` | Максимум байт из одного вложения (обрезка после ответа MCP) |
| `ALLURE_LOGS_MAX_SNIPPET_CHARS` | нет | `65536` | Максимум символов финального `log_snippet` одного теста |
| `ALLURE_CLUSTERING_THRESHOLD` | нет | `0.60` | Порог similarity; ниже — агрессивнее объединение |
| `ALLURE_CLUSTERING_STEP_STRICT_THRESHOLD` | нет | `0.95` | Hard gate по step path (см. `core-clustering.md`); `0.0` отключает, `1.0` режет при любом отличии |
| `ALLURE_LOGS_CLUSTERING_WEIGHT` | нет | `0.15` | Вес log channel в кластеризации |
| `ALLURE_CLUSTERING_LOG_SPLIT_THRESHOLD` | нет | `0.30` | Gate по ошибкам лога (см. `core-clustering.md`, «Кластеризация»); `0.0` отключает |
| `ALLURE_CLUSTERING_RESOURCE_GATE` | нет | `true` | Gate по хостам и локаторам сообщения (см. `core-clustering.md`, «Кластеризация») |
| `ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS` | нет | `2000` | Лимит message в задании |
| `ALLURE_LLM_PROMPT_TRACE_MAX_CHARS` | нет | `400` | Лимит trace в задании |
| `ALLURE_LLM_PROMPT_LOG_MAX_CHARS` | нет | `8000` | Лимит log в задании |
| `ALLURE_RETRY_MAX_DETAIL_REQUESTS` | нет | `200` | Потолок запросов результата по id (без expand) за ошибками попыток на прогон; `0` — не запрашивать |
| `ALLURE_IMAGES_PER_PROBLEM` | нет | `3` | Скриншотов на проблему в задании (0–3); `0` — не скачивать |
| `ALLURE_IMAGES_MAX_BYTES` | нет | `5242880` | Потолок одного скриншота (64 КБ – 20 МБ) |
| `ALLURE_RERUN_ATTEMPTS` | нет | `3` | Перезапуск тестов (`skill-rerun-worker.md`): запусков до первого успеха (1–5) |
| `ALLURE_RERUN_MAX_TESTS` | нет | `20` | Тестов за раз (0–100); `0` выключает перезапуск |
| `ALLURE_RERUN_TIMEOUT` | нет | `1800` | Общий потолок времени перезапуска, с (60–14400) |
| `ALLURE_RERUN_MAVEN_ARGS` | нет | `""` | Свои аргументы Maven (`shlex`), значения не печатаются |
| `ALLURE_RERUN_RESOLVE` | нет | `false` | Статус passed в TestOps прошедшим при перезапуске (после «да»), через REST; по умолчанию выключен: REST закрыт, в MCP записи нет |
| `ALLURE_OPERATION_ID_KEYS` | нет | `""` | Ключи id операции проекта через запятую (`businessId`), к реестру; имя — `[A-Za-z][A-Za-z0-9_.-]{0,63}`, иначе `ConfigurationError` |
