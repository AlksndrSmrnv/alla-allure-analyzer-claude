# CLAUDE.md — alla (AI Test Failure Triage Agent)

## Что это

**alla** — сервис автоматического анализа результатов прогонов автотестов из Allure TestOps.
Конечная цель: группировка падений по уникальным проблемам, определение причины (тест / приложение / окружение / данные), формирование рекомендаций через GigaChat + базу знаний, обновление TestOps.

Сейчас реализованы **MVP Step 1–2**: по ID запуска (launch) забрать результаты тестов из Allure TestOps API, выделить упавшие, кластеризовать по корневой причине, вывести сводку.

## Архитектура

```
alla <launch_id>
  │
  ▼
┌─────────┐     ┌──────────────────┐     ┌───────────────────┐
│  cli.py │────▶│  TriageService   │────▶│ AllureTestOpsClient│──── HTTP ──▶ Allure TestOps API
│(argparse│     │  (бизнес-логика) │     │ (TestResultsProvider│
│ asyncio)│     │  fetch→filter→   │     │  Protocol impl)    │
│         │     │  summarize       │     │                    │
└────┬────┘     └──────────────────┘     └────────┬───────────┘
     │                  │                          │
     │                  ▼                          ▼
     │           TriageReport              AllureAuthManager
     │           (Pydantic model)          (JWT exchange + cache)
     │                  │
     ▼                  ▼
┌──────────────────────────────┐
│    ClusteringService         │
│  (text-first: full error     │
│   text → TF-IDF + cosine →   │
│   agglomerative clustering   │
│   complete linkage, scipy)   │
└──────────────────────────────┘
                │
                ▼
         ClusteringReport
         (Pydantic model)
                │
                ▼
┌──────────────────────────────┐     ┌───────────────────────┐
│    PostgresKnowledgeBase     │────▶│  PostgreSQL           │
│  (KnowledgeBaseProvider      │     │  alla.kb_entry table  │
│   Protocol impl)             │     │  (global + per-project│
│   keyword + TF-IDF matching  │     │   записи)             │
└──────────────────────────────┘     └───────────────────────┘
                │
                ▼
         KB Match Results
         (в рамках кластеров)
                │
                ▼
┌──────────────────────────────┐
│    KBPushService             │
│  (запись рекомендаций KB     │
│   обратно в TestOps через    │
│   POST /api/comment)         │
└──────────────────────────────┘
                │
                ▼
         AllureTestOpsClient
         (TestResultsUpdater
          Protocol impl)
```

### Слои

| Слой | Пакет | Назначение |
|------|-------|------------|
| **CLI** | `alla/cli.py` | Точка входа CLI. argparse + asyncio.run(). Вывод text/json. |
| **HTTP-сервер** | `alla/server.py` | Точка входа REST API. FastAPI + uvicorn. JSON-ответы. |
| **Оркестратор** | `alla/orchestrator.py` | Общий pipeline анализа, вызывается из CLI и сервера. |
| **Сервисы** | `alla/services/` | Бизнес-логика. Не знает про HTTP. Оперирует доменными моделями. |
| **Отчёты** | `alla/report/` | Генерация выходных артефактов (HTML). Не знает про HTTP и бизнес-логику. |
| **Клиенты** | `alla/clients/` | Интеграции с внешними системами. Сейчас — Allure TestOps HTTP API. |
| **База знаний** | `alla/knowledge/` | Хранилище известных ошибок. Protocol + YAML-реализация + TextMatcher. |
| **Модели** | `alla/models/` | Pydantic-модели: API-ответы и доменные объекты. |
| **Конфиг** | `alla/config.py` | Единый `Settings` через pydantic-settings (env vars + .env). |
| **Исключения** | `alla/exceptions.py` | Иерархия ошибок: `AllaError` → `AuthenticationError`, `AllureApiError` и т.д. |

### Ключевой принцип расширяемости

`TestResultsProvider` — это `Protocol` (интерфейс) для чтения данных. `AllureTestOpsClient` его реализует.
Любой будущий источник данных (локальный allure-report, БД, другая TMS) реализует тот же Protocol, и `TriageService` работает с ним без изменений.

`TestResultsUpdater` — отдельный `Protocol` для записи данных обратно в источник. Разделение read/write позволяет реализовать только чтение для источников, не поддерживающих запись. `AllureTestOpsClient` реализует оба протокола.

`CommentManager` — Protocol для чтения и удаления комментариев к тест-кейсам (`GET /api/comment`, `DELETE /api/comment/{id}`). Разделён от `TestResultsUpdater` для backward-compatibility: источники данных, не поддерживающие управление комментариями, не обязаны его реализовывать. `AllureTestOpsClient` реализует все четыре протокола.

`KnowledgeBaseProvider` — аналогичный Protocol для базы знаний. `YamlKnowledgeBase` его реализует.
Будущая реализация через RAG (vector DB) реализует тот же Protocol без изменений в CLI или сервисах.

## Структура файлов

```
├── pyproject.toml                  # PEP 621, зависимости, скрипт alla
├── .env.example                    # Шаблон переменных окружения
├── .gitignore
├── CLAUDE.md                       # Этот файл
└── src/
    └── alla/
        ├── __init__.py             # __version__ = "0.1.0"
        ├── cli.py                  # CLI: argparse → Settings → Auth → Client → Service → Report
        ├── orchestrator.py         # Общий pipeline анализа (CLI + HTTP-сервер)
        ├── server.py               # HTTP-сервер (FastAPI + uvicorn)
        ├── config.py               # Settings(BaseSettings) — все ALLURE_* env vars
        ├── exceptions.py           # AllaError, ConfigurationError, AuthenticationError,
        │                           #   AllureApiError, PaginationLimitError, KnowledgeBaseError
        ├── logging_config.py       # setup_logging() — stdlib logging, формат с timestamp
        ├── models/
        │   ├── common.py           # TestStatus(Enum), PageResponse[T](Generic)
        │   ├── testops.py          # TestResultResponse, LaunchResponse, CommentResponse,
        │   │                       #   FailedTestSummary, TriageReport
        │   ├── clustering.py       # ClusterSignature, FailureCluster, ClusteringReport
        │   └── llm.py              # LLMClusterAnalysis, LLMAnalysisResult,
        │                           #   LLMPushResult, LLMLaunchSummary
        ├── clients/
        │   ├── base.py             # TestResultsProvider(Protocol) — чтение,
        │   │                       #   TestResultsUpdater(Protocol) — запись,
        │   │                       #   CommentManager(Protocol) — чтение/удаление комментариев
        │   ├── auth.py             # AllureAuthManager — JWT exchange через /api/uaa/oauth/token
        │   ├── langflow_client.py  # LangflowClient — HTTP-клиент Langflow с retry/backoff
        │   └── testops_client.py   # AllureTestOpsClient — HTTP клиент (httpx async)
        ├── knowledge/
        │   ├── base.py             # KnowledgeBaseProvider(Protocol) — интерфейс KB
        │   ├── models.py           # KBEntry, KBMatchResult, RootCauseCategory
        │   ├── matcher.py          # TextMatcher — TF-IDF cosine similarity matching
        │   ├── postgres_kb.py      # PostgresKnowledgeBase — реализация KB через PostgreSQL
        │   ├── feedback_store.py   # FeedbackStore(Protocol) — интерфейс обратной связи
        │   ├── feedback_models.py  # Pydantic-модели для feedback API
        │   └── postgres_feedback.py # PostgresFeedbackStore — PostgreSQL-реализация
        ├── utils/
        │   └── text_normalization.py  # normalize_text() — UUID, timestamps, IP → placeholders
        ├── report/
        │   └── html_report.py         # generate_html_report(result, endpoint) — self-contained HTML без внешних зависимостей
        └── services/
            ├── triage_service.py          # TriageService.analyze_launch() — основная логика
            ├── clustering_service.py      # ClusteringService — кластеризация ошибок
            ├── kb_push_service.py         # KBPushService — запись рекомендаций KB в TestOps
            ├── llm_service.py             # LLMService.analyze_clusters(),
            │                              #   generate_launch_summary(), push_llm_results(),
            │                              #   build_cluster_prompt(), build_launch_summary_prompt()
            └── comment_delete_service.py  # CommentDeleteService — удаление комментариев alla
```

## Конфигурация

Все настройки через env vars с префиксом `ALLURE_` или файл `.env` в рабочей директории.

| Переменная | Обязательная | По умолчанию | Описание |
|------------|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps (например `https://allure.company.com`) |
| `ALLURE_TOKEN` | да | — | API-токен (Profile → API Tokens в Allure TestOps) |
| `ALLURE_PROJECT_ID` | да | — | ID проекта в Allure TestOps |
| `ALLURE_LOG_LEVEL` | нет | `INFO` | Уровень логирования: DEBUG, INFO, WARNING, ERROR |
| `ALLURE_SSL_VERIFY` | нет | `true` | Проверка SSL-сертификатов. Для корпоративных сетей — `false` |
| `ALLURE_REQUEST_TIMEOUT` | нет | `30` | Таймаут HTTP-запросов в секундах |
| `ALLURE_PAGE_SIZE` | нет | `100` | Результатов на страницу (пагинация API) |
| `ALLURE_MAX_PAGES` | нет | `50` | Максимум страниц при пагинации (защита от бесконечных циклов) |
| `ALLURE_CLUSTERING_ENABLED` | нет | `true` | Включить/выключить кластеризацию ошибок |
| `ALLURE_CLUSTERING_THRESHOLD` | нет | `0.60` | Порог схожести для кластеризации (0.0–1.0). Ниже = более агрессивное слияние |
| `ALLURE_KB_ENABLED` | нет | `false` | Включить/выключить поиск по базе знаний |
| `ALLURE_KB_POSTGRES_DSN` | нет | `""` | Строка подключения PostgreSQL для KB (например `postgresql://user:pass@localhost:5432/alla_kb`) |
| `ALLURE_KB_MIN_SCORE` | нет | `0.15` | Минимальный score для включения KB-совпадения в отчёт |
| `ALLURE_KB_MAX_RESULTS` | нет | `5` | Максимум KB-совпадений на один кластер |
| `ALLURE_KB_PUSH_ENABLED` | нет | `false` | Записывать рекомендации KB обратно в Allure TestOps через комментарии к тест-кейсам |
| `ALLURE_KB_FEEDBACK_ENABLED` | нет | `false` | Включить систему обратной связи для KB-совпадений (like/dislike, создание записей из HTML-отчёта) |
| `ALLURE_SERVER_HOST` | нет | `0.0.0.0` | Хост для HTTP-сервера (alla-server) |
| `ALLURE_SERVER_PORT` | нет | `8090` | Порт для HTTP-сервера (alla-server) |
| `ALLURE_LLM_ENABLED` | нет | `false` | Включить/выключить LLM-анализ кластеров через Langflow |
| `ALLURE_LANGFLOW_BASE_URL` | нет | `""` | Базовый URL Langflow API |
| `ALLURE_LANGFLOW_FLOW_ID` | нет | `""` | ID flow в Langflow |
| `ALLURE_LANGFLOW_API_KEY` | нет | `""` | API-ключ для Langflow |
| `ALLURE_LLM_TIMEOUT` | нет | `120` | Таймаут одного LLM-запроса в секундах |
| `ALLURE_LLM_CONCURRENCY` | нет | `3` | Макс. параллельных запросов к Langflow |
| `ALLURE_LLM_PUSH_ENABLED` | нет | `false` | Записывать результаты LLM-анализа в TestOps через комментарии |
| `ALLURE_LLM_MAX_RETRIES` | нет | `3` | Число повторных попыток при 429/503/сетевых ошибках Langflow (0 = без retry) |
| `ALLURE_LLM_RETRY_BASE_DELAY` | нет | `1.0` | Базовая задержка в секундах для exponential backoff (delay = base × 2^attempt) |

## Установка и запуск

### Требования

- Python 3.11+
- Доступ к Allure TestOps (URL + API-токен)

### Установка

```bash
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install -e .
```

Для корпоративных сетей с внутренним PyPI:
```bash
pip install -e . --index-url https://nexus.company.com/repository/pypi-proxy/simple/ --trusted-host nexus.company.com
```

### Настройка

```bash
cp .env.example .env
# Отредактировать .env — заполнить ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID
```

### Запуск

```bash
alla 12345                              # текстовый отчёт по launch #12345
alla 12345 --output-format json         # JSON для автоматизации
alla 12345 --output-format json --html-report-file alla-report.html > alla-report.json  # JSON + HTML за один прогон
alla 12345 --log-level DEBUG            # подробные HTTP-логи
alla 12345 --page-size 50               # 50 результатов на страницу
alla --version                          # версия
alla --help                             # справка

alla delete 12345                       # удалить комментарии alla для запуска #12345
alla delete 12345 --dry-run             # предварительный просмотр без удаления
alla delete 12345 --log-level DEBUG     # подробные логи
```

### Запуск HTTP-сервера

```bash
alla-server                             # FastAPI-сервер на 0.0.0.0:8090
# или с настройкой через env vars:
ALLURE_SERVER_PORT=9000 alla-server
# или напрямую через uvicorn:
uvicorn alla.server:app --host 0.0.0.0 --port 8090
```

REST API:

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | `{"status": "ok", "version": "..."}` |
| POST | `/api/v1/analyze/{launch_id}` | Полный pipeline анализа, возвращает JSON |
| DELETE | `/api/v1/comments/{launch_id}` | Удалить комментарии alla для тестов запуска. `?dry_run=true` — предпросмотр |
| GET | `/docs` | Swagger UI (автогенерация FastAPI) |

```bash
# Анализ запуска
curl -X POST http://localhost:8090/api/v1/analyze/12345

# Удаление комментариев alla (предпросмотр)
curl -X DELETE "http://localhost:8090/api/v1/comments/12345?dry_run=true"

# Удаление комментариев alla
curl -X DELETE http://localhost:8090/api/v1/comments/12345

# Health check
curl http://localhost:8090/health
```

### Выходные коды

| Код | Значение |
|-----|----------|
| `0` | Успех |
| `1` | Ошибка выполнения (API недоступен, launch не найден и т.д.) |
| `2` | Ошибка конфигурации (не заданы обязательные env vars, не указан launch_id) |
| `130` | Прервано пользователем (Ctrl+C) |

### Пример вывода (text)

```
=== Отчёт триажа Allure ===
Запуск: #12345 (Nightly Regression Run)
Всего: 847 | Успешно: 801 | Провалено: 30 | Сломано: 12 | Пропущено: 4 | Неизвестно: 0

Падения (42):
  [FAILED]  test_login_with_invalid_credentials (ID: 98765)
            https://allure.company.com/launch/12345/testresult/98765
            Expected status 200 but got 401
  [BROKEN]  test_payment_processing_timeout (ID: 98766)
            https://allure.company.com/launch/12345/testresult/98766

=== Кластеры падений (2 уникальных проблем из 42 падений) ===

╔════════════════════════════════════════════════════════════════════════════════════╗
║ Кластер #1: NullPointerException in UserService.getUser (28 тестов)              ║
║ Пример: Expected non-null value from UserService.getUser()                       ║
║ Тесты: 98765, 98770, 98771, ...                                                  ║
╚════════════════════════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════════════════════════╗
║ Кластер #2: TimeoutError in PaymentGateway.process (14 тестов)                   ║
║ Пример: Connection timed out after 30000ms                                       ║
║ Тесты: 98766, 98780, ...                                                         ║
╚════════════════════════════════════════════════════════════════════════════════════╝
```

## Allure TestOps API

### Аутентификация

JWT через обмен API-токена:
```
POST /api/uaa/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=apitoken&scope=openid&token={ALLURE_TOKEN}
```
Ответ: `{ "access_token": "eyJ...", "expires_in": 3600 }`.
Токен кэшируется, обновляется автоматически за 5 мин до истечения.

### Используемые эндпоинты

| Метод | Путь | Параметры | Назначение |
|-------|------|-----------|------------|
| GET | `/api/launch/{id}` | — | Метаданные запуска |
| GET | `/api/testresult` | `launchId`, `page`, `size`, `sort` | Результаты тестов по запуску (пагинация) |
| GET | `/api/testresult/{id}` | — | Детальный результат теста (fallback для получения `trace`) |
| GET | `/api/testresult/{id}/execution` | — | Дерево шагов выполнения теста (основной источник ошибок) |
| POST | `/api/comment` | JSON body: `{"testCaseId": 1337, "body": "..."}` | Добавление комментария к тест-кейсу (KB push) |
| GET | `/api/comment` | `testCaseId`, `size` | Получение комментариев для тест-кейса (пагинация) |
| DELETE | `/api/comment/{id}` | — | Удаление комментария по ID |

### Пагинация

Ответ API:
```json
{
  "content": [ ... ],
  "totalElements": 847,
  "totalPages": 9,
  "size": 100,
  "number": 0
}
```
Клиент итерирует страницы автоматически. `ALLURE_MAX_PAGES` — защита от бесконечного цикла.

### Обработка ошибок

- **401** — автоматический retry: сброс JWT, повторная аутентификация, повтор запроса.
- **404** — подсказка: "Check Swagger UI at {endpoint}/swagger-ui.html".
- Неизвестные поля в ответе API — молча принимаются (`extra="allow"` в Pydantic-моделях).

## Модели данных

### TestResultResponse (сырой ответ API)

```python
id: int                          # ID результата
name: str | None                 # Имя теста
full_name: str | None            # Полное имя (alias: fullName)
status: str | None               # passed / failed / broken / skipped / unknown
status_details: dict | None      # { "message": "...", "trace": "..." } (alias: statusDetails)
trace: str | None                # Top-level trace (доступен на GET /api/testresult/{id})
duration: int | None             # Длительность в мс
test_case_id: int | None         # ID тест-кейса (alias: testCaseId)
launch_id: int | None            # ID запуска (alias: launchId)
category: str | None             # Категория ошибки
muted: bool                      # Замьючен ли тест
hidden: bool                     # Скрыт (retry, не последний)
# + любые дополнительные поля (extra="allow")
```

### TriageReport (выходная доменная модель)

```python
launch_id: int
launch_name: str | None
project_id: int | None               # ID проекта (из GET /api/launch/{id} → projectId)
total_results: int
passed_count / failed_count / broken_count / skipped_count / unknown_count: int
failed_tests: list[FailedTestSummary]   # Только failed + broken
failure_count: int                       # @property: failed + broken
```

### FailedTestSummary (каждый упавший тест)

```python
test_result_id: int
name: str
full_name: str | None
status: TestStatus               # FAILED или BROKEN
category: str | None
status_message: str | None       # Трёхуровневый fallback (см. ниже)
status_trace: str | None         # Трёхуровневый fallback (см. ниже)
execution_steps: list[ExecutionStep] | None  # Дерево шагов выполнения
test_case_id: int | None
link: str | None                 # URL на тест в Allure TestOps
duration_ms: int | None
```

**Извлечение ошибки — трёхуровневый fallback:**
1. Из execution-шагов (`GET /api/testresult/{id}/execution`) — рекурсивный обход дерева.
2. Из `statusDetails` результата (пагинированный список `GET /api/testresult`).
3. Из top-level `trace` индивидуального результата (`GET /api/testresult/{id}`).
   Срабатывает только если шаги 1-2 не дали результата. Первая строка trace → `status_message`.

### ClusteringReport (результат кластеризации)

```python
launch_id: int
total_failures: int
cluster_count: int
clusters: list[FailureCluster]
unclustered_count: int
```

### FailureCluster (кластер ошибок)

```python
cluster_id: str                  # SHA-256 hash сигнатуры (16 символов)
label: str                       # "NullPointerException in UserService.getUser"
signature: ClusterSignature      # exception_type, message_pattern, common_frames, category
member_test_ids: list[int]       # ID тестов в кластере
member_count: int
example_message: str | None      # status_message первого теста
example_trace_snippet: str | None
```

## Дизайн-решения

1. **`extra="allow"` в API-моделях** — Allure TestOps API не полностью задокументирован публично. Неизвестные поля принимаются без ошибок валидации. Это критично для совместимости с разными версиями TestOps.

2. **`populate_by_name=True`** — API возвращает camelCase (`fullName`, `testCaseId`), Python-модели используют snake_case. Alias + populate_by_name позволяет конструировать модели из обоих форматов.

3. **Все поля Optional (кроме id)** — разные версии API и конфигурации могут возвращать разные наборы полей. Optional с дефолтами предотвращает падение при отсутствии поля.

4. **Async (httpx.AsyncClient)** — готовность к параллельным запросам в будущих фазах (fetch результатов + fetch логов + fetch KB одновременно).

5. **Deferred imports в cli.py** — тяжёлые импорты (httpx, pydantic) не загружаются при `alla --help` / `alla --version`.

6. **Endpoint paths как class attributes** — `AllureTestOpsClient.LAUNCH_ENDPOINT`, `TESTRESULT_ENDPOINT` можно переопределить в подклассе, если API-структура другой версии TestOps отличается.

7. **Кластеризация: text-first подход** — весь текст ошибки (message + trace + category) берётся целиком, без разбора на типы исключений или фреймы стек-трейса. TF-IDF с Unicode-совместимой токенизацией (`(?u)\b\w\w+\b`) работает с любым языком (латиница, кириллица, смешанный) и любым форматом ошибок. Agglomerative clustering (complete linkage) через scipy гарантирует, что каждая пара тестов в кластере ближе порога. Никаких захардкоженных regex для конкретных языков/фреймворков. Подробнее см. раздел «Алгоритм кластеризации».

8. **Трёхуровневый fallback извлечения ошибки** — некоторые тесты падают, но execution steps все в статусе passed. Цепочка: (1) execution-шаги `GET /api/testresult/{id}/execution`, (2) `statusDetails` из пагинированного списка, (3) `GET /api/testresult/{id}` → top-level `trace`. Третий уровень делает HTTP-запрос **только** для тестов без ошибки после шагов 1-2, что минимизирует нагрузку.

## Алгоритм кластеризации

Реализация: `alla/services/clustering_service.py`.

### Принцип: text-first

Ошибки не разбираются на составные части (exception type, stack frames, category). Вместо этого весь доступный текст ошибки берётся целиком и сравнивается как текст. Это делает алгоритм **универсальным**: работает с Java, Python, Go, .NET, кириллицей, латиницей, смешанным текстом, нестандартными форматами.

### Общая схема

1. **Сборка документа** — из каждого `FailedTestSummary` собирается один текстовый документ: конкатенация `status_message` + `status_trace` + `category` (что есть).

2. **Нормализация** — минимальная замена волатильных данных, уникальных для конкретного запуска:
   - UUID (с дефисами и без, 32 hex-символа) → `<ID>`
   - Timestamps → `<TS>`
   - Длинные числа (4+ цифр) → `<NUM>`
   - IP-адреса → `<IP>`

3. **Фильтрация** — тесты без текста (нет message, нет trace, нет category) автоматически становятся singleton-кластерами.

4. **TF-IDF** (`sklearn.TfidfVectorizer`) — тексты векторизуются. Unicode-совместимый token_pattern `(?u)\b\w\w+\b` ловит слова на любом языке. Общие слова получают низкий IDF и не раздувают схожесть. Bigrams (`ngram_range=(1,2)`) захватывают устойчивые фразы.

5. **Cosine distance** — `1.0 - cosine_similarity(tfidf_a, tfidf_b)`. Единственная метрика, без композитных весов и разбивки на сигналы.

6. **Agglomerative clustering** — `scipy.cluster.hierarchy.linkage(method="complete")` + `fcluster(criterion="distance", t=distance_threshold)`. Complete linkage гарантирует, что **максимальное** расстояние между любыми двумя тестами внутри кластера < порога.

### Настройка кластеризации

| Параметр | Env var | По умолчанию | Эффект изменения |
|----------|---------|:---:|---|
| Порог схожести | `ALLURE_CLUSTERING_THRESHOLD` | `0.60` | **Выше** (0.70–0.80) — более строгие кластеры, больше singleton'ов. **Ниже** (0.40–0.50) — более агрессивное слияние, меньше кластеров. |
| Вкл/выкл | `ALLURE_CLUSTERING_ENABLED` | `true` | `false` — кластеризация полностью отключена, выводятся только отдельные тесты. |

**Рекомендации по тюнингу:**
- Если слишком много мелких кластеров → понизить `ALLURE_CLUSTERING_THRESHOLD` до 0.50.
- Если разные ошибки всё ещё попадают в один кластер → повысить до 0.70–0.80.

### Внутренние параметры (ClusteringConfig)

Дополнительные параметры доступны только программно (не через env vars):

| Параметр | По умолчанию | Назначение |
|----------|:---:|---|
| `tfidf_max_features` | 1000 | Размер словаря TF-IDF |
| `tfidf_ngram_range` | (1, 2) | N-граммы: unigrams + bigrams |
| `max_label_length` | 120 | Максимальная длина метки кластера |

## База знаний (KB)

Реализация: `alla/knowledge/`.

### Назначение

Хранилище известных ошибок с критериями сопоставления и рекомендациями по устранению. После кластеризации для каждого кластера ищутся релевантные записи KB. Результаты выводятся внутри рамок кластеров.

### PostgreSQL-хранилище

Записи хранятся в таблице `alla.kb_entry` в PostgreSQL. Поддерживаются глобальные записи (`project_id IS NULL`) и записи для конкретного проекта (`project_id = N`). ID проекта извлекается из ответа `GET /api/launch/{id}` (поле `projectId`) и передаётся в `PostgresKnowledgeBase` при инициализации.

### Модели данных KB

**KBEntry** — запись базы знаний:
```python
id: str                          # Уникальный slug
title: str                       # Название проблемы
description: str                 # Подробное описание
error_example: str               # Пример ошибки из лога (для TF-IDF сопоставления)
category: RootCauseCategory      # test / service / env / data
resolution_steps: list[str]      # Шаги по устранению
```

**KBMatchResult** — результат сопоставления:
```python
entry: KBEntry                   # Совпавшая запись KB
score: float                     # 0.0–1.0
matched_on: list[str]            # Объяснение: что именно совпало
```

### Алгоритм сопоставления (TextMatcher)

Нечёткий TF-IDF cosine similarity в `alla/knowledge/matcher.py`:

1. **Нормализация** — и `error_example` из KB, и текст ошибки нормализуются: UUID → `<ID>`, timestamps → `<TS>`, длинные числа → `<NUM>`, IP → `<IP>`. Используется общий модуль `alla/utils/text_normalization.py`.

2. **TF-IDF vectorization** — все документы (query + KB examples + KB title/desc) векторизуются `TfidfVectorizer(token_pattern=r"(?u)\b\w\w+\b", ngram_range=(1,2), max_features=500)`.

3. **Cosine similarity** — для каждой KB-записи: similarity с `error_example` (example_sim) и с `title + description` (title_desc_sim).

4. **Blended score** = 0.8 × example_sim + 0.2 × title_desc_sim. Фильтрация по `min_score` (default 0.15), лимит `max_results` (default 5).

### Структура таблицы KB

Таблица `alla.kb_entry` — основное хранилище. Поле `error_example` — фрагмент ошибки из лога для TF-IDF сопоставления:

```sql
CREATE TABLE alla.kb_entry (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    error_example TEXT NOT NULL,
    category    TEXT NOT NULL,           -- test / service / env / data
    resolution_steps JSONB NOT NULL DEFAULT '[]',
    project_id  INTEGER,                 -- NULL = глобальная запись
    UNIQUE (slug, project_id)
);
```

### Миграция на RAG

При миграции на vector DB (RAG):
- `error_example` → текст для генерации embedding
- `category` → metadata filter в vector DB
- `resolution_steps`, `title`, `description` → payload
- `KnowledgeBaseProvider` Protocol остаётся без изменений

## Что сделано (MVP Step 1–3)

- [x] CLI с аргументами и двумя форматами вывода (text / json)
- [x] JWT-аутентификация с кэшированием и автообновлением
- [x] HTTP-клиент с автопагинацией и retry на 401
- [x] Pydantic-модели устойчивые к неизвестным полям API
- [x] Protocol-based интерфейс клиента для расширяемости
- [x] Фильтрация failed/broken тестов
- [x] Сводка со ссылками на Allure TestOps UI
- [x] Конфигурация через env vars / .env файл
- [x] Отключение SSL-верификации для корпоративных сетей (`ALLURE_SSL_VERIFY=false`)
- [x] **Кластеризация падений** — text-first подход: весь текст ошибки берётся целиком (без разбора на exception type / frames). TF-IDF с Unicode-токенизацией + agglomerative clustering (complete linkage) через scipy/scikit-learn. Универсально: работает с любым языком, форматом ошибок, кириллицей.
- [x] **База знаний (KB)** — PostgreSQL-хранилище известных ошибок с рекомендациями. `KnowledgeBaseProvider` Protocol для расширяемости. Нечёткий TF-IDF cosine similarity matching по полю `error_example` (большой фрагмент ошибки из лога). Нормализация волатильных данных (UUID, timestamps, числа, IP) обеспечивает устойчивость к различиям между запусками. Поддерживаются глобальные записи (`project_id IS NULL`) и per-project записи. ID проекта берётся из `GET /api/launch/{id}` → `projectId`.
- [x] **Fallback получения trace** — трёхуровневая цепочка извлечения ошибки: execution steps → statusDetails → `GET /api/testresult/{id}` (top-level `trace`). Покрывает случай, когда все шаги execution passed, а ошибка только в индивидуальном результате.
- [x] **Нормализация UUID без дефисов** — 32-символьные hex-строки (session ID и т.п.) нормализуются в `<ID>` наравне со стандартными UUID.
- [x] **KB Push в TestOps** — запись рекомендаций KB обратно в Allure TestOps через `POST /api/comment` (комментарий к тест-кейсу). `TestResultsUpdater` Protocol для write-операций. `KBPushService` с дедупликацией по test_case_id, параллельными запросами и per-test error resilience. Управляется настройкой `ALLURE_KB_PUSH_ENABLED` (по умолчанию выключено).
- [x] **HTTP-сервер** — REST API через FastAPI + uvicorn. `POST /api/v1/analyze/{launch_id}` запускает полный pipeline и возвращает JSON. Общая логика вынесена в `orchestrator.py`, используется и CLI, и сервером. Swagger UI на `/docs`. Настройки `ALLURE_SERVER_HOST` / `ALLURE_SERVER_PORT`.
- [x] **Удаление комментариев alla** — команда `alla delete <launch_id>` сканирует комментарии к failed/broken тестам запуска, фильтрует по префиксу `[alla]` в теле комментария и удаляет через `DELETE /api/comment/{id}`. `CommentManager` Protocol для чтения/удаления комментариев. `CommentDeleteService` с двухфазным алгоритмом (scan → delete), semaphore-based concurrency и per-test error resilience. Флаг `--dry-run` для предварительного просмотра без удаления. REST API: `DELETE /api/v1/comments/{launch_id}?dry_run=true`.
- [x] **LLM-анализ кластеров (Langflow)** — `LLMService.analyze_clusters()` отправляет каждый кластер в Langflow с контекстом (ошибка + трейс + KB-совпадения + лог). Ответ — 4 секции: что произошло / категория / что делать / критичность. Параллелизм через semaphore (`ALLURE_LLM_CONCURRENCY`). Клиент: `clients/langflow_client.py` с exponential backoff retry (`ALLURE_LLM_MAX_RETRIES`, `ALLURE_LLM_RETRY_BASE_DELAY`). LLM push (`ALLURE_LLM_PUSH_ENABLED`): комментарии `[alla] LLM-анализ ошибки` к тест-кейсам. При включённом LLM — KB push не выполняется (LLM включает KB в промпт).
- [x] **Итоговый LLM-отчёт по прогону** — `LLMService.generate_launch_summary()` делает один дополнительный LLM-вызов после `analyze_clusters()`. Промпт содержит метаданные запуска + все кластеры (с их per-cluster анализами если доступны). LLM пишет 2-4 абзаца: общая картина → ключевые проблемы по убыванию критичности → приоритетные действия. CLI: секция `=== Итоговый отчёт ===` после кластерных рамок. Модель: `LLMLaunchSummary` в `models/llm.py`.
- [x] **HTML-отчёт** — `alla/report/html_report.py`, `generate_html_report(result, endpoint)`. Self-contained HTML (pure Python, без Jinja2/внешних зависимостей): заголовок прогона, стат-карточки, итоговый LLM-summary, карточки кластеров (LLM-анализ + KB-совпадения + ссылки на тесты в Allure TestOps). Флаг CLI `--html-report-file PATH` — генерирует JSON и HTML за один прогон (без повторных API-вызовов). Jenkinsfile: `publishHTML()` прикрепляет отчёт к сборке как browsable artifact. Требует Jenkins-плагин **HTML Publisher**.

## Что не сделано (план на следующие фазы)

- [ ] **GigaChat LLM** — языковая модель для формулировки рекомендаций. Добавить `alla/llm/` с `LLMProvider` Protocol. Модель получает короткий сжатый контекст (не сырые данные), большая часть работы — детерминистическая.
- [ ] **Обновление TestOps (расширение)** — запись дополнительных полей (category, comment) обратно в Allure TestOps. Сейчас реализована запись `description` через KB Push.
- [ ] **Корреляция с логами** — подключение логов приложения по transactionId/correlationId. Добавить `alla/clients/log_client.py` с `LogProvider` Protocol.
- [ ] **Интеграция с дефект-трекером** — создание/привязка багов в Jira/другой системе.
- [ ] **Автоматические действия** — ремедиация, перезапуск тестов.
- [ ] **Тесты** — pytest + pytest-asyncio + pytest-httpx. Зависимости в `[project.optional-dependencies] dev`.
- [ ] **RAG для KB** — миграция базы знаний на vector DB для семантического поиска при росте KB. `KnowledgeBaseProvider` Protocol остаётся без изменений.

## Зависимости

### Runtime

| Пакет | Версия | Зачем |
|-------|--------|-------|
| httpx | >=0.27,<1.0 | Async HTTP клиент |
| pydantic | >=2.5,<3.0 | Валидация данных, API-модели |
| pydantic-settings | >=2.1,<3.0 | Конфиг из env vars + .env |
| scikit-learn | >=1.4,<2.0 | TF-IDF векторизация, cosine similarity для кластеризации и KB matching |
| fastapi | >=0.110,<1.0 | HTTP-сервер (REST API) |
| uvicorn | >=0.29,<1.0 | ASGI-сервер для FastAPI |
| psycopg | >=3.1,<4.0 | PostgreSQL-клиент для KB бэкенда |

### Dev (опциональные)

```bash
pip install -e ".[dev]"
```

pytest, pytest-asyncio, pytest-httpx, ruff, mypy.

## Работа в IDE

### IntelliJ IDEA / PyCharm

1. Открыть корневую папку проекта
2. Правый клик на `src/` → **Mark Directory as → Sources Root**
3. **Settings → Project → Python Interpreter** → выбрать venv с установленным пакетом
4. В терминале IDE: `pip install -e .`
5. **File → Invalidate Caches → Invalidate and Restart**

### VS Code

Открыть корневую папку. Python-расширение автоматически подхватит venv из `.venv/`.
