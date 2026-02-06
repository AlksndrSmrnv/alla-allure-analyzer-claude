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
```

### Слои

| Слой | Пакет | Назначение |
|------|-------|------------|
| **CLI** | `alla/cli.py` | Точка входа. argparse + asyncio.run(). Вывод text/json. |
| **Сервисы** | `alla/services/` | Бизнес-логика. Не знает про HTTP. Оперирует доменными моделями. |
| **Клиенты** | `alla/clients/` | Интеграции с внешними системами. Сейчас — Allure TestOps HTTP API. |
| **Модели** | `alla/models/` | Pydantic-модели: API-ответы и доменные объекты. |
| **Конфиг** | `alla/config.py` | Единый `Settings` через pydantic-settings (env vars + .env). |
| **Исключения** | `alla/exceptions.py` | Иерархия ошибок: `AllaError` → `AuthenticationError`, `AllureApiError` и т.д. |

### Ключевой принцип расширяемости

`TestResultsProvider` — это `Protocol` (интерфейс). `AllureTestOpsClient` его реализует.
Любой будущий источник данных (локальный allure-report, БД, другая TMS) реализует тот же Protocol, и `TriageService` работает с ним без изменений.

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
        ├── config.py               # Settings(BaseSettings) — все ALLURE_* env vars
        ├── exceptions.py           # AllaError, ConfigurationError, AuthenticationError,
        │                           #   AllureApiError, PaginationLimitError
        ├── logging_config.py       # setup_logging() — stdlib logging, формат с timestamp
        ├── models/
        │   ├── common.py           # TestStatus(Enum), PageResponse[T](Generic)
        │   ├── testops.py          # TestResultResponse, LaunchResponse,
        │   │                       #   FailedTestSummary, TriageReport
        │   └── clustering.py       # ClusterSignature, FailureCluster, ClusteringReport
        ├── clients/
        │   ├── base.py             # TestResultsProvider(Protocol) — интерфейс
        │   ├── auth.py             # AllureAuthManager — JWT exchange через /api/uaa/oauth/token
        │   └── testops_client.py   # AllureTestOpsClient — HTTP клиент (httpx async)
        └── services/
            ├── triage_service.py      # TriageService.analyze_launch() — основная логика
            └── clustering_service.py  # ClusteringService — кластеризация ошибок
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
alla 12345 --log-level DEBUG            # подробные HTTP-логи
alla 12345 --page-size 50               # 50 результатов на страницу
alla --version                          # версия
alla --help                             # справка
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
=== Allure Triage Report ===
Launch: #12345 (Nightly Regression Run)
Total: 847 | Passed: 801 | Failed: 30 | Broken: 12 | Skipped: 4 | Unknown: 0

Failures (42):
  [FAILED]  test_login_with_invalid_credentials (ID: 98765)
            https://allure.company.com/launch/12345/testresult/98765
            Expected status 200 but got 401
  [BROKEN]  test_payment_processing_timeout (ID: 98766)
            https://allure.company.com/launch/12345/testresult/98766

=== Failure Clusters (2 unique problems from 42 failures) ===

╔════════════════════════════════════════════════════════════════════════════════════╗
║ Cluster #1: NullPointerException in UserService.getUser (28 tests)               ║
║ Cluster ID: a1b2c3d4e5f60789                                                     ║
║ Example: Expected non-null value from UserService.getUser()                      ║
║ Tests: 98765, 98770, 98771, ...                                                  ║
╚════════════════════════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════════════════════════╗
║ Cluster #2: TimeoutError in PaymentGateway.process (14 tests)                    ║
║ Cluster ID: 0f9e8d7c6b5a4321                                                     ║
║ Example: Connection timed out after 30000ms                                      ║
║ Tests: 98766, 98780, ...                                                         ║
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
| GET | `/api/testresult` | `launchId`, `page`, `size`, `sort` | Результаты тестов по запуску |

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
status_message: str | None       # Из statusDetails.message
status_trace: str | None         # Из statusDetails.trace
execution_steps: list[ExecutionStep] | None  # Дерево шагов выполнения
test_case_id: int | None
link: str | None                 # URL на тест в Allure TestOps
duration_ms: int | None
```

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

## Алгоритм кластеризации

Реализация: `alla/services/clustering_service.py`.

### Принцип: text-first

Ошибки не разбираются на составные части (exception type, stack frames, category). Вместо этого весь доступный текст ошибки берётся целиком и сравнивается как текст. Это делает алгоритм **универсальным**: работает с Java, Python, Go, .NET, кириллицей, латиницей, смешанным текстом, нестандартными форматами.

### Общая схема

1. **Сборка документа** — из каждого `FailedTestSummary` собирается один текстовый документ: конкатенация `status_message` + `status_trace` + `category` (что есть).

2. **Нормализация** — минимальная замена волатильных данных, уникальных для конкретного запуска:
   - UUID → `<ID>`
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

## Что сделано (MVP Step 1–2)

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

## Что не сделано (план на следующие фазы)

- [ ] **База знаний (KB)** — хранилище известных паттернов ошибок + решений. Добавить пакет `alla/knowledge/`. Поиск по statusMessage и category.
- [ ] **GigaChat LLM** — языковая модель для формулировки рекомендаций. Добавить `alla/llm/` с `LLMProvider` Protocol. Модель получает короткий сжатый контекст (не сырые данные), большая часть работы — детерминистическая.
- [ ] **Обновление TestOps** — запись причины/рекомендации обратно в Allure TestOps. Расширить `AllureTestOpsClient` методами PATCH/POST.
- [ ] **Корреляция с логами** — подключение логов приложения по transactionId/correlationId. Добавить `alla/clients/log_client.py` с `LogProvider` Protocol.
- [ ] **Интеграция с дефект-трекером** — создание/привязка багов в Jira/другой системе.
- [ ] **Автоматические действия** — ремедиация, перезапуск тестов.
- [ ] **Тесты** — pytest + pytest-asyncio + pytest-httpx. Зависимости в `[project.optional-dependencies] dev`.

## Зависимости

### Runtime

| Пакет | Версия | Зачем |
|-------|--------|-------|
| httpx | >=0.27,<1.0 | Async HTTP клиент |
| pydantic | >=2.5,<3.0 | Валидация данных, API-модели |
| pydantic-settings | >=2.1,<3.0 | Конфиг из env vars + .env |
| scikit-learn | >=1.4,<2.0 | TF-IDF векторизация, cosine similarity для кластеризации |

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
