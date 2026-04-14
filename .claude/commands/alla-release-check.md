Проверь, что новая фича в alla готова к мержу. Пройди по каждому пункту и поставь ✅ / ❌ / N/A.

## Архитектура

- [ ] Новый внешний контракт вынесен в правильный слой: `src/alla/clients/base.py` для async HTTP/TestOps-клиентов; `src/alla/knowledge/base.py` или `src/alla/knowledge/feedback_store.py` для KB/storage-контрактов
- [ ] Для основного analysis pipeline новая логика подключена через `src/alla/orchestrator.py`; для вспомогательных флоу используется осознанное подключение через `src/alla/server.py`, `src/alla/app_support.py`, store или отдельный service
- [ ] Если фича затрагивает HTML-отчёт, ссылки на отчёт или feedback UI, обновлены `src/alla/report/html_report.py`, `src/alla/app_support.py` и при необходимости `src/alla/server.py`
- [ ] Новая настройка безопасна по умолчанию: либо `False`, либо пустое значение/отключённый backend, либо явное условие активации через конфиг

## Код

- [ ] I/O-bound публичные методы новых клиентов и сетевых сервисов — `async def`; чистые CPU/transform helper'ы могут оставаться sync
- [ ] Fan-out к внешним ресурсам ограничен через `asyncio.Semaphore` или эквивалентный throttling
- [ ] Новые модели внешнего API в `src/alla/models/` используют `populate_by_name=True` и `extra="allow"`; optional-поля добавлены там, где источник реально может их не прислать
- [ ] Новые доменные модели/dataclass'ы не ломают текущие контракты `AnalysisResult`, CLI JSON и HTTP JSON
- [ ] Новые исключения наследуют от `AllaError` в `src/alla/exceptions.py`
- [ ] Нет hardcoded URL, токенов, DSN, путей и фича-флагов вне `Settings`

## Слои и связность

- [ ] Сетевой доступ сосредоточен в `clients/`; PostgreSQL storage — в `knowledge/` или `report/`
- [ ] Сервисный код не тянет зависимости из entrypoint-слоя (`argparse`, `print`, FastAPI, `HTTPException`)
- [ ] `cli.py` и analysis-роуты `server.py` используют `orchestrator.analyze_launch()` для основного pipeline; служебные операции (`alla delete`, feedback, merge rules, reports) идут через свои service/store helper'ы

## Документация

- [ ] Обновлены релевантные разделы `CLAUDE.md`: архитектура, структура файлов, конфигурация, команды запуска или HTTP endpoints
- [ ] `.env.example` содержит все новые пользовательские `ALLURE_*` переменные с актуальными комментариями
- [ ] Если изменился JSON/HTML/API-контракт, обновлены примеры и тесты

## Ручная проверка

- [ ] `alla --help` отрабатывает без ошибок
- [ ] `alla --version` отрабатывает
- [ ] Базовый сценарий `alla {launch_id}` работает без новой конфигурации
- [ ] Если затронут резолв запуска по имени — проверен `alla --launch-name ...`
- [ ] Если затронуты комментарии — проверен `alla delete {launch_id} --dry-run`
- [ ] Если затронут сервер/HTML/report links — проверены `/health`, `/api/v1/analyze/{launch_id}` и нужный служебный endpoint

---

Для каждого ❌ укажи: что нарушено, почему это важно и в каком файле исправить.
