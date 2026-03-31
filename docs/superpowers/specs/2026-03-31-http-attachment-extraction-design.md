# Дизайн: извлечение HTTP-контекста из аттачментов тестов

**Дата:** 2026-03-31
**Статус:** Утверждён

## Цель

Alla сейчас извлекает только `[ERROR]`-блоки из текстовых логов (`text/plain`). Тест-фреймворки прикладывают также HTTP-запросы и ответы — JSON, XML, текстовые дампы — которые содержат корреляционные идентификаторы (`RqUID`, `OperUID`) и тело ошибочных ответов (4xx/5xx). Эти данные нужно включать в `log_snippet`, чтобы улучшить качество кластеризации и LLM-анализа.

## Ограничения

- Структура аттачментов произвольна: разные проекты — разные схемы JSON/XML.
- MIME-тип и имя файла ненадёжны как единственный критерий → нужна контент-based детекция.
- Аттачменты варьируются от десятков KB до нескольких MB — нужен лимит на размер для JSON-парсинга.
- Публичный интерфейс `LogExtractionService` не меняется.
- Результат идёт в существующее поле `FailedTestSummary.log_snippet`.

## Архитектура

### Расширение фильтра MIME-типов

Текущий фильтр: только `text/plain`.
Новый: `text/plain` + `application/json` + `text/json` + `application/xml` + `text/xml`.

### Два пути обработки одного аттачмента

```
for att in attachments:
    content_bytes = download(att)
    if MIME == text/plain:
        → _extract_error_blocks(text)           # существующий лог-экстрактор
        → _detect_and_extract_http(content_bytes, att)  # HTTP-детекция тоже
    if MIME is json / xml:
        → _detect_and_extract_http(content_bytes, att)  # только HTTP-экстрактор
```

Для `text/plain` оба экстрактора запускаются безусловно — дамп HTTP-запроса на `text/plain` может содержать и `[ERROR]`-строки, и корреляционные ID. Каждый возвращает пустую строку если ничего не нашёл, в `log_snippet` добавляются только непустые секции.

### `_detect_and_extract_http(content_bytes, att) -> str`

1. Если `len(content_bytes) > http_max_bytes` → только regex-поиск (без JSON-парсинга).
2. Если MIME — JSON или контент начинается с `{` / `[` → `json.loads` → `_scan_json_for_http_info`.
3. Если не JSON или `json.loads` упал → `_extract_text_http_info` (regex по тексту).
4. Возвращает пустую строку, если ничего не найдено (не захламляем `log_snippet`).

### `_scan_json_for_http_info(obj) -> str`

Рекурсивный обход JSON-дерева, глубина ≤ 10.

**Собирает correlation IDs** (ключи, case-insensitive):
`rquid`, `operuid`, `requestid`, `correlationid`, `traceid`

**Собирает HTTP-статус** (ключи `status`, `statuscode`, `httpstatus`, `responsestatus`):
только если значение — целое число 400–599.

**Собирает поля ошибок** (ключи, case-insensitive):
`error`, `errorcode`, `errormessage`, `fault`, `faultcode`, `message`, `description`, `reason`, `details`, `cause`

Логика включения `message`: включается только если рядом есть признак ошибки (status 4xx/5xx или поле `error`/`fault` с непустым значением), иначе слишком много ложных срабатываний.

### `_extract_text_http_info(text) -> str`

Regex по сырому тексту (работает когда JSON не распарсился или файл > лимита):

- Корреляция: `RqUID\s*[=:"]\s*(\S+)`, `OperUID\s*[=:"]\s*(\S+)` (case-insensitive)
- HTTP статус: `HTTP/[12]\.\d\s+(4\d\d|5\d\d)\b`
- Ошибки: `"error"\s*:\s*"([^"]{1,200})"`, `"message"\s*:\s*"([^"]{1,200})"`, `"fault[^"]*"\s*:\s*"([^"]{1,200})"`

### Формат вывода в `log_snippet`

```
--- [HTTP: response.json] ---
Корреляция: RqUID=abc123, OperUID=def456
HTTP статус: 500
error: Service unavailable
message: Database connection failed
```

Секция добавляется только если нашлось хотя бы одно из: correlation ID, HTTP статус ≥ 400, поле ошибки.

## Конфигурация

Одно новое поле в `Settings` (`config.py`):

| Переменная | Дефолт | Описание |
|---|---|---|
| `ALLURE_HTTP_MAX_BYTES` | `524288` (512 KB) | Лимит размера аттачмента для JSON-парсинга. Файлы больше → только regex. |

Передаётся через `LogExtractionConfig.http_max_bytes`.

## Что не меняется

- `FailedTestSummary` — поле `log_snippet` остаётся, тип `str | None`.
- Публичный метод `LogExtractionService.enrich_with_logs` — сигнатура без изменений.
- Кластеризация, LLM-промпт, KB-поиск — читают `log_snippet` как раньше.
- `ALLURE_LOGS_CLUSTERING_WEIGHT` — управляет весом `log_snippet` в кластеризации без изменений.

## Файлы затронуты

| Файл | Изменение |
|---|---|
| `src/alla/config.py` | + `http_max_bytes: int` |
| `src/alla/services/log_extraction_service.py` | Расширить фильтр MIME, добавить `_detect_and_extract_http`, `_scan_json_for_http_info`, `_extract_text_http_info`, обновить `LogExtractionConfig` |
| `src/alla/orchestrator.py` | Прокинуть `http_max_bytes` в `LogExtractionConfig` |
| `tests/services/test_log_extraction.py` | Новые unit-тесты для HTTP-экстракторов |
