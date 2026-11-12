---
paths:
  - "qwen-skill/alla-launch/scripts/alla_core/clients/**"
  - "qwen-skill/alla-launch/scripts/alla_core/config.py"
  - "qwen-skill/alla-launch/scripts/alla_core/credentials.py"
  - "qwen-skill/alla-launch/scripts/qwen_credentials.mjs"
  - "qwen-skill/alla-launch/tests/test_core_credentials.py"
  - "qwen-skill/alla-launch/scripts/alla_core/credentials.py"
  - "qwen-skill/alla-launch/scripts/qwen_credentials.mjs"
  - "qwen-skill/alla-launch/credentials/**"
  - "qwen-skill/alla-launch/scripts/alla_core/exceptions.py"
  - "qwen-skill/alla-launch/scripts/alla_core/models/testops.py"
  - "qwen-skill/alla-launch/tests/test_core_testops_client.py"
  - "qwen-skill/alla-launch/tests/test_core_mcp_client.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/projects.py"
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

- Один инструмент — `testops_find_testresults`, он ищет **в проекте**: настоящий TestOps
  требует `projectId` (пользователь, 2026-11-06). Проект — `TestOpsMcpClient(settings,
  project_id=…)` (выбирает `cli`/`projects.py` по `ALLURE_PROJECT_ID`, `skill-protocol-workspace.md`)
  и идёт в каждый вызов (`_arguments`): список прогона, полный результат, результат без expand.
  Имя параметра — из схемы (`projectId`/`project_id`/`project`; схемы нет — `projectId`); в
  схеме параметра нет — не шлётся (старый сервер). Параметр в `required`, а проекта нет —
  `McpError(no_project)` при открытии сеанса (`errors.fetch_error_hint` объясняет формат
  `ALLURE_PROJECT_ID`); `project_param`/`project_required` клиента читает `check`. Фейковый
  TestOps требует `projectId` так же, а на чужой проект отвечает пустой страницей. Список прогона — `aql: "launch = <id>"` со
  страницами, без expand. Полный результат — `aql: "id = <id>"` строго с **одним** id (со
  списком expand не работает) и `expand: ["scenario", "fixtures", "attachments"]`; ответ —
  результат с `scenario` (дерево шагов, вложения в `steps[]`), `fixtures` и словарём
  `attachments` `{"<id>": {..., "content", "contentLength"}}`: текст (`text/plain`,
  `text/html`) — строкой как есть, бинарное (`image/png`) — base64 в том же `content`,
  `contentLength` — размер исходника.
- Имена параметров (aql, страница, размер, expand) — из `inputSchema` (`_tool_params`,
  кандидаты `_PARAM_CANDIDATES`); схемы нет — `aql`/`page`/`size`/`expand`. Нет параметра
  страницы — одна страница, даже если ответ говорит о других (запрос без номера вернул бы ту
  же; ревью Codex). Страница ответа: `content`/`items`/`results`/`data` или список; размер
  страницы — `size` ответа, иначе запрошенный, а неполная страница при итоге больше
  полученного — урезанная сервером: дальше страницы считаются его размером
  (`_server_page_size`), иначе терялся хвост прогона (ревью Codex); итогов
  (`totalPages`/`totalElements`/`total`) нет — страницы, пока приходят новые id (пустая
  или повтор прежней — конец, повтор в результаты не идёт), `last_total_elements` тогда
  `None`: короткая страница без итогов ещё не конец, иначе выгрузка обрывалась на первой
  урезанной странице (ревью Grok). Есть только `totalPages` — листаем по нему, но
  `last_total_elements` тоже `None`: вычисленное число выдало бы ложную «потерю» (ревью Codex).
- Транспорт — свой Streamable HTTP на `httpx` (пакет `mcp` не берём: веб-сервер и десяток
  зависимостей в lock): JSON-RPC в POST, `Authorization: api-token <token>`,
  `Accept: application/json, text/event-stream`, `Mcp-Session-Id` из ответа `initialize`,
  `MCP-Protocol-Version` после него (заголовки — на каждую попытку). Ответ JSON читается
  целиком; поток SSE (`_read_sse`, `httpx.stream`) — по строкам (`aiter_lines` режет и по
  одиночному `\r`), до своего ответа, затем закрывается: сервер не обязан закрывать поток
  сам; запрос сервера в потоке (`ping`) получает ответ отдельным POST (`ping` — `{}`, прочее —
  -32601), иначе сервер, ждущий ответа на ping, держал бы `tools/call` до таймаута (ревью
  Grok). Версия из `initialize` — только `SUPPORTED_VERSIONS` (Streamable HTTP: 2025-03-26,
  2025-06-18, 2025-11-25), иначе `McpError(protocol)`. `initialize` → `notifications/initialized`
  → `tools/list` (с `nextCursor`) лениво, при первом вызове; `connect()` — то же явно
  (`check`). **Сеанс забыт** (404 при открытом сеансе): одно новое открытие на всех
  получивших 404 — по поколению сеанса (`_generation`) под `_init_lock`, остальные ждут и
  повторяют в новом; старый id заменяется только ответом нового `initialize` (параллельные
  запросы не уходят без `Mcp-Session-Id`), сбой открытия — `_params = None`, следующий вызов
  откроет заново (ревью Grok); 404 и после нового сеанса — `McpError(protocol)`, а не «нет
  сервера». Ошибки — `McpError(reason)`: `not_found` (404 без сеанса), `transport` (405:
  старый SSE-транспорт не поддерживается), `no_tool`, `tool` (`isError`, JSON-RPC error),
  `protocol`; 401/403 и прочие HTTP — `AllureApiError` со статусом (`errors.fetch_error_hint`
  различает их; сеть — подсказка про `mcp_endpoint` и `ALLURE_MCP_URL`, если он задан).
- **Повторы при временных сбоях.** Все вызовы MCP читают, поэтому повторяются как GET прежнего
  клиента: до `http_retries` раз при `RETRY_STATUSES` (429, 502, 503, 504) и
  `httpx.RequestError`; пауза — `Retry-After` в секундах, иначе 1, 2, 4… с, не больше 30 с
  (`clients/retry.py`). 500 не повторяется. Последний сбой сети — `AllureApiError(0, …)`.
  Причина: один 502 балансировщика при пагинации обрывал весь `prepare`. Тесты подменяют
  `_sleep` клиента.
- **Один запрос на результат.** Полный ответ кешируется (`_full`, блокировка на id):
  `get_test_result_execution`, `get_test_result_detail`, `get_attachments_for_test_result` и
  `get_attachment_content` его переиспользуют. Шаги: `scenario` (`{steps}`, список,
  `{content}`; обёртка с любым полем шага — имя, статус, ошибка, вложения — сама корень,
  иначе failed-корень с passed-подшагами терял путь упавшего шага; только голая
  `{steps: [...]}` — контейнер, ревью Codex), фикстуры before — в начало дерева, after
  (`type`/ключ с `after`, `teardown`) — в конец; вложения шага (id или объект) — метаданными
  из словаря, без `content`. `message`/`trace` верхнего уровня без `statusDetails` — в
  `statusDetails` (`_result`). Без expand (`aql: id = N`) — `get_test_result_detail` для
  ошибок скрытых попыток, если полного ответа нет.
- **Содержимое вложений** (`_decode`): текстовый MIME — строка как есть, другой MIME — base64;
  без MIME — base64, только если раскодированное похоже на файл (сигнатура PNG/JPEG/GIF/RIFF/
  PDF/ZIP или нулевые байты): «test» — правильный base64, а лог без типа должен остаться
  логом. Хранится не больше `max(logs_max_attachment_bytes, images_max_bytes + 1)` байт, в
  пределах `content_budget` (128 МБ, LRU); вытесненное запрашивается заново тем же полным
  запросом результата (`_owner`: вложение → результат) и отдаётся из этого ответа, а не из
  памяти: вложения одного результата больше бюджета иначе вытесняли нужное до чтения (ревью
  Codex). Неизвестное вложение — 404.
- `get_attachment_content(id, max_bytes=None)` — потолок по умолчанию
  `logs_max_attachment_bytes`; скриншоты скилл берёт со своим (`images_max_bytes` + 1 байт:
  длиннее потолка — обрезан, пропускается). Протокол `AttachmentProvider` параметра не
  требует: фейковые провайдеры логов его не знают.
- **Прогон** (`get_launch`): инструмента нет — из первой страницы результатов (кешируется для
  `get_all_test_results_for_launch`): имя — `launchName` или `launch.name`, проект —
  `projectId` (иначе проект клиента), `closed` — `launch.closed`, если сервер их прислал; иначе `None`. Время прогона
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
чужие ключи игнорирует. Непустой `ALLURE_TOKEN` в `.env` запрещён даже при токене окружения.
Токен берётся из непустой переменной окружения (CI), иначе — из sensitive setting
`ALLURE_TOKEN` расширения Qwen `alla-launch-credentials` (workspace важнее user).
Границы значений — `BOUNDS` (ge/le). `load(require_testops=False)` — для команд без TestOps
(`rerun`): адрес и токен не обязательны, хранилище не читается; запрет открытого токена
в `.env` остаётся.

`credentials.py` вызывает `qwen_credentials.mjs` через subprocess без shell, с закрытым
pipe и очищенным окружением. Адаптер поддерживает npm Qwen Code 0.27.0 и Node.js в PATH;
внутренние API Qwen читают настройки и secret store, шифрование в Python не дублируется.
QWEN_HOME учитывается. Не вызывать refreshCache/recovery, загрузку hooks/commands или
полную загрузку расширений ради чтения токена. Токен из `.env` проекта/расширения Qwen
отклоняется, чтобы не принять plaintext fallback Qwen. Значение и сырые ошибки адаптера
не попадают в сообщения; отсутствие секрета, неизвестная версия и сбой хранилища —
ConfigurationError без продолжения запроса к TestOps.
На macOS используем Bun из npm-установки Qwen, если он есть: в sandbox Node
os.userInfo() недоступен, а native Bun-reader Qwen получает ту же OS identity через id.
Runtime стартует из каталога пакета Qwen; для Bun отключены config, dotenv, auto-install
и macros. Проект не должен выполнять preload до чтения секретов.
Происхождение ALLURE_TOKEN из .env/settings.env проверяется по служебной provenance Qwen;
это не CI export. Проверка raw деклараций не даёт скрыть токен пустым повтором ключа.
Загрузчик .env вообще не добавляет ALLURE_TOKEN в результат: разрешённая пустая запись
или комментарий после неё не должны стать токеном вместо хранилища Qwen.

Qwen предпочитает keychain, иначе хранит AES-256-GCM в extension-secrets-v1.json (0600).
Файловый ключ выводится из фиксированной строки и host/user: не обещать keychain-only
или защиту от восстановления ключа при известных host/user. Штатный keychain-reader
Qwen может создавать/удалять временную запись проверки доступности.

| Переменная | Обязательная | По умолчанию | Описание |
|---|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps без `/api` |
| `ALLURE_TOKEN` | да | хранилище Qwen | API token; env CI важнее Qwen, в `.env` запрещён |
| `ALLURE_MCP_URL` | нет | `""` | Адрес MCP-сервера TestOps; пусто — `<ALLURE_ENDPOINT>/api/mcp`; иначе `http(s)://` |
| `ALLURE_PROJECT_ID` | нет | `""` | Проект TestOps поиска результатов: `5` или по модулям `orders=5,payments/api=7` (`Settings.projects` → `ProjectRef`; номер > 0, модуль — папка без `..`, `\` → `/`, не повторяется; число вместе с парами — `ConfigurationError`) |
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

## Политика адресов TestOps

`Settings.load(project_root=...)` читает `[testops].allowed_origins` только из проектного
`alla-extensions/config.toml`. Exact scheme+IDNA/lowercase host+effective port; IP canonical.
Missing/empty/malformed deny. Env не расширяет policy. REST/MCP/auth проверяют origin на
границе клиента даже для direct Settings, follow_redirects=False, 3xx — ошибка.
`require_testops=False` (локальный rerun) policy не требует. Расширения сохраняют [testops]
при изменении enabled. Recorder принимает --project-root для той же политики.
