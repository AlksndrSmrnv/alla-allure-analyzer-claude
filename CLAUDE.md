# CLAUDE.md — alla (AI Test Failure Triage Agent)

## Что это

**alla** — сервис автоматического анализа упавших автотестов из Allure TestOps. По ID запуска (launch) забирает результаты, кластеризует падения по корневой причине, ищет совпадения в базе знаний, запускает LLM-анализ, записывает рекомендации обратно в TestOps, генерирует HTML-отчёт.

## Архитектура

### Pipeline (порядок внутри оркестратора)

`TriageService` → `LogExtractionService` → `ClusteringService` → KB search → `LLMService` → KB/LLM push

### Слои

| Пакет | Назначение |
|-------|------------|
| `alla/cli.py` | CLI: argparse + asyncio. Вывод text/json. |
| `alla/server.py` | HTTP API: FastAPI + uvicorn. |
| `alla/orchestrator.py` | Общий pipeline (CLI + сервер). `analyze_launch()` → `AnalysisResult`. |
| `alla/services/` | Бизнес-логика без HTTP. |
| `alla/clients/` | Allure TestOps + GigaChat. Протоколы в `base.py`. |
| `alla/knowledge/` | База знаний: Protocol + PostgreSQL + TextMatcher. |
| `alla/report/` | Генерация HTML-отчётов. |
| `alla/models/` | Pydantic-модели API и доменных объектов. |
| `alla/config.py` | `Settings(BaseSettings)` — все `ALLURE_*` env vars. |

### Протоколы (`alla/clients/base.py`)

`AllureTestOpsClient` реализует все четыре: `TestResultsProvider` (чтение), `TestResultsUpdater` (запись комментариев), `CommentManager` (чтение/удаление комментариев), `AttachmentProvider` (скачивание вложений). Разделение — чтобы новые источники данных реализовывали только нужное.

## Структура файлов

```
src/alla/
├── cli.py, orchestrator.py, server.py, config.py, exceptions.py
├── models/
│   ├── testops.py      # TestResultResponse, FailedTestSummary, TriageReport
│   ├── clustering.py   # FailureCluster, ClusteringReport
│   └── llm.py          # LLMClusterAnalysis, LLMLaunchSummary
├── clients/
│   ├── base.py         # Все Protocol-интерфейсы
│   ├── auth.py         # AllureAuthManager — JWT exchange + cache
│   ├── testops_client.py
│   └── gigachat_client.py
├── knowledge/
│   ├── base.py, models.py      # KBEntry, KBMatchResult, RootCauseCategory
│   ├── matcher.py              # TextMatcher — TF-IDF cosine similarity
│   ├── postgres_kb.py          # PostgresKnowledgeBase
│   └── postgres_feedback.py    # PostgresFeedbackStore
├── utils/text_normalization.py  # normalize_text() — UUID/timestamps/IP → placeholders
├── report/
│   ├── html_report.py    # generate_html_report() — self-contained HTML
│   └── report_store.py   # PostgresReportStore — таблица alla.report
└── services/
    ├── triage_service.py, clustering_service.py
    ├── kb_push_service.py, llm_service.py
    ├── log_extraction_service.py, comment_delete_service.py
```

## Конфигурация

Все переменные с префиксом `ALLURE_` (файл `.env` в рабочей директории).

| Переменная | Обязательная | По умолчанию | Описание |
|------------|:---:|---|---|
| `ALLURE_ENDPOINT` | да | — | URL Allure TestOps |
| `ALLURE_TOKEN` | да* | `""` | API-токен TestOps |
| `ALLURE_PROJECT_ID` | да | — | ID проекта в TestOps |
| `ALLURE_VAULT_URL` | нет | `""` | Vault Proxy URL для получения секретов |
| `ALLURE_SSL_VERIFY` | нет | `true` | `false` для корпоративных сетей |
| `ALLURE_LOG_LEVEL` | нет | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `ALLURE_REQUEST_TIMEOUT` | нет | `30` | Таймаут HTTP-запросов (сек) |
| `ALLURE_PAGE_SIZE` | нет | `100` | Результатов на страницу |
| `ALLURE_MAX_PAGES` | нет | `50` | Защита от бесконечной пагинации |
| `ALLURE_CLUSTERING_THRESHOLD` | нет | `0.60` | Порог схожести (0–1). Ниже = агрессивнее |
| `ALLURE_KB_POSTGRES_DSN` | нет | `""` | DSN PostgreSQL для базы знаний. **KB включается автоматически когда задан.** |
| `ALLURE_KB_MIN_SCORE` | нет | `0.15` | Мин. score KB-совпадения |
| `ALLURE_KB_MAX_RESULTS` | нет | `5` | Макс. KB-совпадений на кластер |
| `ALLURE_PUSH_TO_TESTOPS` | нет | `true` | Записывать результаты в TestOps |
| `ALLURE_GIGACHAT_BASE_URL` | нет | `""` | Базовый URL GigaChat. **LLM включается автоматически когда заданы `BASE_URL` + `CERT_B64` + `KEY_B64`.** |
| `ALLURE_GIGACHAT_CERT_B64` | нет | `""` | Клиентский сертификат PEM в base64 для mTLS |
| `ALLURE_GIGACHAT_KEY_B64` | нет | `""` | Приватный ключ PEM в base64 для mTLS |
| `ALLURE_GIGACHAT_MODEL` | нет | `"GigaChat-2-Max"` | Модель GigaChat для LLM-анализа |
| `ALLURE_LLM_TIMEOUT` | нет | `120` | Таймаут LLM-запроса (сек) |
| `ALLURE_LLM_CONCURRENCY` | нет | `3` | Параллельных запросов к GigaChat |
| `ALLURE_LLM_MAX_RETRIES` | нет | `3` | Retry при 429/503 |
| `ALLURE_LLM_RETRY_BASE_DELAY` | нет | `1.0` | Базовая задержка backoff (сек) |
| `ALLURE_LOGS_CLUSTERING_WEIGHT` | нет | `0.15` | Вес лог-канала в кластеризации. Логи скачиваются автоматически если есть вложения `text/plain`. |
| `ALLURE_REPORTS_DIR` | нет | `""` | Директория для HTML-отчётов |
| `ALLURE_REPORTS_POSTGRES` | нет | `false` | Хранить отчёты в PostgreSQL |
| `ALLURE_SERVER_HOST` | нет | `0.0.0.0` | Хост HTTP-сервера |
| `ALLURE_SERVER_PORT` | нет | `8090` | Порт HTTP-сервера |
| `ALLURE_SERVER_EXTERNAL_URL` | нет | `""` | Внешний URL сервера для ссылок в TestOps |

## Запуск

```bash
pip install -e .
cp .env.example .env  # заполнить ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID
```

### CLI

```bash
alla 12345                          # анализ запуска, текстовый вывод + HTML
alla 12345 --output-format json     # JSON-вывод
alla 12345 --log-level DEBUG
alla delete 12345                   # удалить комментарии alla
alla delete 12345 --dry-run
```

### HTTP-сервер

```bash
alla-server   # слушает на 0.0.0.0:8090
```

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Health check |
| GET | `/api/v1/launch/resolve` | Резолв имени запуска → ID (`?name=&project_id=`) |
| POST | `/api/v1/analyze/{launch_id}` | Pipeline анализа → JSON |
| POST | `/api/v1/analyze/{launch_id}/html` | Pipeline анализа → HTML-отчёт |
| GET | `/reports/{filename}` | Отдать сохранённый HTML-отчёт |
| DELETE | `/api/v1/comments/{launch_id}` | Удалить комментарии alla (`?dry_run=true`) |
| GET | `/docs` | Swagger UI |

## Ключевые дизайн-решения

1. **API-модели устойчивы к изменениям TestOps** — `extra="allow"` (неизвестные поля молча игнорируются), `populate_by_name=True` (camelCase и snake_case оба принимаются), все поля Optional (кроме id).
2. **Трёхуровневый fallback извлечения ошибки** — (1) execution steps, (2) statusDetails, (3) `GET /api/testresult/{id}`. Третий запрос только если 1-2 не дали результата.
3. **Кластеризация: text-first** — весь текст ошибки целиком, без разбора на типы исключений. TF-IDF + agglomerative clustering (complete linkage). Работает с любым языком и форматом.
4. **KB matching: blended score** — `0.8 × example_sim + 0.2 × title_desc_sim`. Нормализация UUID/timestamps/IP для устойчивости к различиям между запусками.
5. **LLM включает KB в промпт** — при включённом LLM отдельный KB push не выполняется.

## Gotchas

- `_request()` обрабатывает пустое тело ответа (PATCH/DELETE → None). `_request_raw()` — отдельный метод для бинарных вложений.
- Клиент должен быть открыт (`async with`) для любых HTTP-операций, включая KB push.
- В user-facing тексте писать «база знаний», не «KB».
