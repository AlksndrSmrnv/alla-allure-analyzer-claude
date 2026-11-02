---
paths:
  - "qwen-skill/alla-launch/scripts/alla_core/clients/**"
  - "qwen-skill/alla-launch/scripts/alla_core/config.py"
  - "qwen-skill/alla-launch/scripts/alla_core/exceptions.py"
  - "qwen-skill/alla-launch/scripts/alla_core/models/testops.py"
  - "qwen-skill/alla-launch/tests/test_core_testops_client.py"
  - "qwen-skill/alla-launch/tests/test_core_auth.py"
---

# Ядро: клиент TestOps и настройки

## Клиент TestOps

- API-модели принимают неизвестные поля (`extra="allow"`) и оба стиля имён
  (`populate_by_name=True`): payloads TestOps плавают.
- `_request()` должен корректно обрабатывать пустые тела PATCH/DELETE; `_request_raw()` — для
  бинарных attachments.
- **Повторы при временных сбоях.** GET (JSON и stream) повторяется до `http_retries` раз при
  `RETRY_STATUSES` (429, 502, 503, 504) и `httpx.RequestError` (сеть, таймаут): пауза —
  `Retry-After` в секундах, иначе 1, 2, 4… с, не больше 30 с (`clients/retry.py`); каждая
  попытка — со своим повтором при 401. 500 не повторяется (ошибка TestOps на запрос обычно
  воспроизводится), не-GET — тоже. Последний ответ отдаётся как раньше: 5xx — `AllureApiError`
  со статусом, сеть — `AllureApiError(0, …)`. Обмен токена (`AllureAuthManager`,
  `retries=settings.http_retries`) повторяется так же (POST обмена идемпотентен; 401 — нет).
  Причина: один 502 балансировщика при пагинации обрывал весь `prepare`. Тесты подменяют
  `_sleep` клиента и менеджера.
- `get_attachment_content(id, max_bytes=None)` — потолок по умолчанию
  `logs_max_attachment_bytes`; скриншоты скилл качает со своим (`images_max_bytes` + 1 байт:
  длиннее потолка — обрезан, пропускается). Протокол `AttachmentProvider` параметра не
  требует: фейковые провайдеры логов его не знают.

## Настройки (`config.py`)

`Settings` читает `<skill>/.env` и переменные окружения `ALLURE_*` (окружение важнее файла),
чужие ключи игнорирует. Границы значений — `BOUNDS` (ge/le). `load(require_testops=False)` — для
команд без TestOps (`rerun`): адрес и токен не обязательны, остальное проверяется.

| Переменная | Обязательная | По умолчанию | Описание |
|---|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps без `/api` |
| `ALLURE_TOKEN` | да | `""` | API token |
| `ALLURE_REQUEST_TIMEOUT` | нет | `30` | HTTP timeout к TestOps |
| `ALLURE_HTTP_RETRIES` | нет | `3` | Повторы GET при 429/502/503/504 и сбоях сети (0–10) |
| `ALLURE_PAGE_SIZE` | нет | `100` | Размер страницы TestOps API |
| `ALLURE_MAX_PAGES` | нет | `200` | Защита пагинации; больше страниц — `PaginationLimitError` сразу после первой страницы |
| `ALLURE_DETAIL_CONCURRENCY` | нет | `10` | Параллелизм details/comments |
| `ALLURE_LOGS_CONCURRENCY` | нет | `5` | Параллелизм скачивания attachments |
| `ALLURE_LOGS_MAX_ATTACHMENT_BYTES` | нет | `10485760` | Максимум байт из одного вложения (stream-чтение с обрезкой) |
| `ALLURE_LOGS_MAX_SNIPPET_CHARS` | нет | `65536` | Максимум символов финального `log_snippet` одного теста |
| `ALLURE_CLUSTERING_THRESHOLD` | нет | `0.60` | Порог similarity; ниже — агрессивнее объединение |
| `ALLURE_CLUSTERING_STEP_STRICT_THRESHOLD` | нет | `0.95` | Hard gate по step path (см. `core-clustering.md`); `0.0` отключает, `1.0` режет при любом отличии |
| `ALLURE_LOGS_CLUSTERING_WEIGHT` | нет | `0.15` | Вес log channel в кластеризации |
| `ALLURE_CLUSTERING_LOG_SPLIT_THRESHOLD` | нет | `0.30` | Gate по ошибкам лога (см. `core-clustering.md`, «Кластеризация»); `0.0` отключает |
| `ALLURE_CLUSTERING_RESOURCE_GATE` | нет | `true` | Gate по хостам и локаторам сообщения (см. `core-clustering.md`, «Кластеризация») |
| `ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS` | нет | `2000` | Лимит message в задании |
| `ALLURE_LLM_PROMPT_TRACE_MAX_CHARS` | нет | `400` | Лимит trace в задании |
| `ALLURE_LLM_PROMPT_LOG_MAX_CHARS` | нет | `8000` | Лимит log в задании |
| `ALLURE_RETRY_MAX_DETAIL_REQUESTS` | нет | `200` | Потолок `GET /api/testresult/{id}` за ошибками попыток на прогон; `0` — не запрашивать |
| `ALLURE_IMAGES_PER_PROBLEM` | нет | `3` | Скриншотов на проблему в задании (0–3); `0` — не скачивать |
| `ALLURE_IMAGES_MAX_BYTES` | нет | `5242880` | Потолок одного скриншота (64 КБ – 20 МБ) |
| `ALLURE_RERUN_ATTEMPTS` | нет | `3` | Перезапуск тестов (`skill-rerun-worker.md`): запусков до первого успеха (1–5) |
| `ALLURE_RERUN_MAX_TESTS` | нет | `20` | Тестов за раз (0–100); `0` выключает перезапуск |
| `ALLURE_RERUN_TIMEOUT` | нет | `1800` | Общий потолок времени перезапуска, с (60–14400) |
| `ALLURE_RERUN_MAVEN_ARGS` | нет | `""` | Свои аргументы Maven (`shlex`), значения не печатаются |
| `ALLURE_RERUN_RESOLVE` | нет | `true` | Статус passed в TestOps прошедшим при перезапуске (после «да»); `false` — только чтение |
| `ALLURE_OPERATION_ID_KEYS` | нет | `""` | Ключи id операции проекта через запятую (`businessId`), к реестру; имя — `[A-Za-z][A-Za-z0-9_.-]{0,63}`, иначе `ConfigurationError` |
