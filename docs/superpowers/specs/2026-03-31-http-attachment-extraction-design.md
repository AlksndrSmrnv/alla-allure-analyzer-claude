# Дизайн: извлечение HTTP-контекста из аттачментов тестов

**Дата:** 2026-03-31
**Статус:** Утверждён

## Цель

Исторически Alla извлекала только `[ERROR]`-блоки из текстовых логов
(`text/plain`). Тест-фреймворки прикладывают также HTTP-запросы и ответы —
JSON, XML, NDJSON и текстовые дампы — которые содержат корреляционные
идентификаторы (`RqUID`, `OperUID`, `requestId`, `correlationId`, `traceId`) и
тело ошибочных ответов (4xx/5xx). Эти данные включаются в `log_snippet` или
`correlation_hint`, чтобы улучшить качество кластеризации, поиска в базе
знаний и LLM-анализа.

## Ограничения

- Структура аттачментов произвольна: разные проекты — разные схемы JSON/XML.
- MIME-тип и имя файла ненадёжны → нужна content-based детекция типа.
- Аттачменты варьируются от десятков KB до нескольких MB.
- Публичный интерфейс `LogExtractionService` не меняется.
- Результат идёт в существующее поле `FailedTestSummary.log_snippet`.

## Новые зависимости

| Библиотека | Назначение | Примечание |
|---|---|---|
| `python-magic` | Content-based определение типа по байтам | Требует системный пакет `libmagic` в окружении (Linux: `apt install libmagic1`) |
| `charset-normalizer` | Определение кодировки и декодирование | Уже в зависимостях `requests`; использовать `from_bytes(content).best()` |
| `ijson` | Потоковый парсинг больших JSON | Предпочитает бинарный ввод; поддерживает `multiple_values=True` для NDJSON |

**Риски:**

- `python-magic` — thin wrapper над `libmagic`; в CI/CD образ должен включать системный пакет. Magic-инстанс не шарим между потоками.
- `ijson` работает на валидных JSON-потоках; если вложение — смесь логов и куска JSON — парсер упадёт с ошибкой. Regex-fallback остаётся обязательным. `multiple_values=True` помогает только для настоящего NDJSON, не для произвольного мусора.

## Архитектура

### Пайплайн обработки аттачмента

```text
bytes
→ detect_content_type(content[:2048], fallback_mime)
→ if binary (не text/json/xml): skip
→ decode via charset-normalizer when text is needed
→ если json-like: stream parse via ijson → _scan_json_for_http_signals
→ если parse fail или не json: _collect_text_http_signals (regex)
→ если text/plain: также _extract_error_blocks
```

### Два канала для одного аттачмента

```text
for att in all_attachments:
    content_bytes = download(att)
    detected_type = _detect_content_type(content_bytes, fallback_mime=att.mime)

    if text/plain:
        → _extract_error_blocks(text)
        → _extract_http_artifacts(content_bytes, text=text)
    if json / xml:
        → _extract_http_artifacts(content_bytes)
    if binary:
        → skip
```

Для `text/plain` оба экстрактора запускаются безусловно — дамп HTTP-запроса может содержать и `[ERROR]`-строки, и корреляционные ID. Каждый возвращает пустую строку если ничего не нашёл; в `log_snippet` добавляются только непустые секции.

### `_extract_http_artifacts(content_bytes, content_type, text=None) -> tuple[str, str | None]`

1. Попытаться потоковый парсинг через `ijson` (если тип json-like):
   - `ijson.items(BytesIO(content_bytes), "", multiple_values=True)`
   - Для каждого top-level объекта → `_scan_json_for_http_signals(obj)`
2. Если `ijson` упал или тип не JSON → `_collect_text_http_signals(text)` (regex).
3. Возвращает HTTP-секцию только при наличии error-signal: HTTP 4xx/5xx или
   error/fault/cause/reason field.
4. Возвращает `correlation_hint` отдельно даже если HTTP-секция не создана.

### `_scan_json_for_http_signals(obj) -> _HttpSignals`

Рекурсивный обход JSON-дерева, глубина ≤ 10.

**Собирает correlation IDs** (ключи, case-insensitive):
`rquid`, `operuid`, `requestid`, `correlationid`, `traceid`

**Собирает HTTP-статус** (ключи `status`, `statuscode`, `httpstatus`, `responsestatus`):
только если значение — целое число 400–599.

**Собирает поля ошибок** (ключи, case-insensitive):
`error`, `errorcode`, `errormessage`, `fault`, `faultcode`, `faultstring`,
`cause`, `reason`

**Собирает контекстные поля**:
`message`, `description`, `details`

Контекстные поля выводятся только если в итоговом сигнале есть error-signal.
Корреляция без ошибки не попадает в `log_snippet`, но сохраняется в
`FailedTestSummary.correlation_hint`.

### `_collect_text_http_signals(text) -> _HttpSignals`

Regex по сырому тексту — fallback для невалидного JSON и смешанных текстовых дампов:

- Корреляция: JSON/KV/XML формы для `RqUID`, `OperUID`, `requestId`,
  `correlationId`, `traceId` (case-insensitive)
- HTTP статус: `HTTP/[12]\.\d\s+(4\d\d|5\d\d)\b`
- Ошибки: JSON/XML поля `error*`, `fault*`, `cause`, `reason`
- Контекст: JSON поля `message`, `description`, `details`

### Декодирование текста

Заменяем `content.decode("utf-8", errors="replace")` на:

```python
from charset_normalizer import from_bytes

match = from_bytes(content_bytes).best()
if match is None:
    return ""  # бинарный или нераспознанный файл
text = str(match)
```

### Формат вывода в `log_snippet`

```text
--- [HTTP: response.json] ---
Корреляция: RqUID=abc123, OperUID=def456
HTTP статус: 500
error: Service unavailable
message: Database connection failed
```

HTTP-секция добавляется только если нашлось хотя бы одно из: HTTP статус ≥ 400
или поле ошибки. Корреляция без ошибки сохраняется как отдельная подсказка
кластера, но не создаёт самостоятельную HTTP-секцию.

## Конфигурация

Поле `ALLURE_HTTP_MAX_BYTES` из предыдущей версии спека **удаляется** — с `ijson` лимит на размер для выбора стратегии не нужен. Парсинг потоковый, regex-fallback срабатывает на ошибке парсера, а не на размере.

## Что не меняется

- `FailedTestSummary` — поле `log_snippet`, тип `str | None`.
- Публичный метод `LogExtractionService.enrich_with_logs` — сигнатура без изменений.
- Кластеризация, LLM-промпт, KB-поиск — читают `log_snippet` как раньше.
- `ALLURE_LOGS_CLUSTERING_WEIGHT` — вес `log_snippet` в кластеризации.

## Файлы затронуты

| Файл | Изменение |
|---|---|
| `pyproject.toml` | + `python-magic`, `charset-normalizer`, `ijson` в зависимости |
| `src/alla/services/log_extraction_service.py` | content-based детекция, charset-normalizer decode, ijson parse, `_detect_and_extract_http`, `_scan_json_for_http_info`, `_extract_text_http_info` |
| `src/alla/config.py` | Без изменений (HTTP_MAX_BYTES не нужен) |
| `src/alla/orchestrator.py` | Без изменений |
| `tests/test_log_extraction_service.py` | Unit-тесты для HTTP-экстракторов |

## За рамками этого PR (бэклог)

- **Drain3 / template mining** — отдельный слой над кластеризацией.
- **sentence-transformers + HDBSCAN** — замена текущей кластеризации целиком.
- **Scoring + incident bundle** — изменение структуры `log_snippet` и LLM-промпта координированно.
- **PyOD / IsolationForest** — нужен накопленный baseline.
- **Presidio / tiktoken** — токен-бюджет и PII, отдельная фича.
- **Magika** — апгрейд детектора, если `python-magic` будет давать ошибки на нестандартных форматах.
