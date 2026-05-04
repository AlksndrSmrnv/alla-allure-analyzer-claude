---
name: alla
description: Анализ упавших автотестов из Allure TestOps через MCP-сервер alla. Используй, когда пользователь упоминает «alla», «allure testops», «триаж тестов», просит «проанализировать прогон/launch», прислать отчёт по падениям, найти корневую причину упавших тестов, посмотреть кластеры падений или совпадения с базой знаний.
---

# alla — MCP-триаж упавших тестов Allure TestOps

`alla` — сервис анализа запусков Allure TestOps. MCP endpoint живёт внутри
запущенного `alla-server` и вызывает тот же pipeline, что REST API: триаж,
логи, кластеризация, merge rules, база знаний, LLM и опциональный push в
TestOps.

## Когда использовать

Используй MCP-инструменты `alla`, если пользователь:

- даёт числовой launch ID и просит проанализировать прогон;
- спрашивает, почему упали тесты в конкретном запуске;
- просит основные кластеры, совпадения с базой знаний или LLM-вывод;
- хочет ссылку на HTML-отчёт по launch ID.

Если пользователь дал только имя запуска, MCP-инструменты не умеют резолвить имя.
Сначала используй REST `/api/v1/launch/resolve` или read-only helper из
`skill/alla-analysis`.

## MCP-эндпоинт

`alla-server` монтирует streamable HTTP transport по адресу:

```text
http://<host>:8090/mcp
```

Проверка health:

```bash
curl http://localhost:8090/health
```

Ожидаемый ответ содержит `status=ok`, `version` и `mcp=true`.

## Инструменты

| Инструмент | Когда вызывать | Что возвращает |
|---|---|---|
| `analyze_launch(launch_id, push_to_testops?)` | Нужна компактная сводка без HTML | `launch_id`, `launch_name`, `total_failed`, `clusters_count`, `clusters`, опционально `llm_launch_summary` |
| `analyze_launch_html(launch_id, push_to_testops?)` | Пользователь просит отчёт/ссылку | Всё из `analyze_launch`, плюс `report_filename`, опционально `report_url` или `hint` |

`push_to_testops` переопределяет `ALLURE_PUSH_TO_TESTOPS` для одного вызова:

- `None` — использовать конфиг сервера;
- `false` — read-only анализ без комментариев в TestOps;
- `true` — разрешить запись, если сервер и pipeline настроены.

`analyze_launch_html` сохраняет HTML в `ALLURE_REPORTS_DIR` и/или PostgreSQL,
если эти хранилища включены. Если публичный URL нельзя построить, tool вернёт
`hint`; передай его пользователю.

## Подключение клиента

Пример для MCP-клиента с HTTP transport:

```json
{
  "mcpServers": {
    "alla": {
      "type": "http",
      "url": "http://localhost:8090/mcp"
    }
  }
}
```

Минимальная конфигурация самого сервера:

```env
ALLURE_ENDPOINT=https://allure.example.com
ALLURE_TOKEN=...
```

Для HTML-ссылок нужны `ALLURE_REPORTS_DIR` + `ALLURE_SERVER_EXTERNAL_URL`
или `ALLURE_REPORTS_POSTGRES=true` + `ALLURE_SERVER_EXTERNAL_URL`.

## Как отвечать пользователю

- Отвечай на языке пользователя; для русских запросов — по-русски.
- Пиши «база знаний», не «KB».
- Сначала дай короткий итог запуска, затем самые крупные/опасные кластеры.
- Для каждого важного кластера называй размер, шаг/сигнатуру, лучшее
  совпадение с базой знаний и следующий практический шаг.
- Не вставляй длинные LLM-вердикты целиком: перескажи и отдели факты от
  гипотез.
- Если есть `report_url`, сделай ссылку кликабельной.
- Если есть `hint`, передай его явно.

## Ошибки

- `alla 401` — неверный или отсутствующий `ALLURE_TOKEN` на стороне сервера.
- `alla 404` — launch не найден или имя не резолвится.
- `alla 400` — ошибка конфигурации запроса/сервера.
- `alla 500` — ошибка базы знаний или storage.
- `alla 502` — TestOps недоступен, вернул не-2xx или сработал лимит пагинации.
- `/mcp` даёт redirect/не отвечает — проверь, что используется путь `/mcp`;
  сервер переписывает exact `/mcp` в mounted `/mcp/`, но старые клиенты могут
  требовать trailing slash.
