Проверь, что новая фича в alla готова к мержу. Пройди по каждому пункту и поставь ✅ / ❌ / N/A.

## Архитектура

- [ ] Новые внешние зависимости добавлены в `pyproject.toml` (раздел `[project.dependencies]`)
- [ ] Новые `ALLURE_*` переменные добавлены в `src/alla/config.py` + `.env.example` + `CLAUDE.md`
- [ ] Новые Protocol-интерфейсы (если нужны) добавлены в `src/alla/clients/base.py` или `src/alla/knowledge/base.py`
- [ ] Новая фича opt-in: по умолчанию `False` / `disabled`

## Код

- [ ] Все публичные методы новых сервисов — `async def`
- [ ] Параллельные запросы контролируются через `asyncio.Semaphore` (не голый `gather` с неограниченным N)
- [ ] Новые Pydantic-модели: все поля `Optional` с `None` дефолтом (кроме `id`)
- [ ] Модели из внешних API: `extra="allow"`, `populate_by_name=True`
- [ ] Новые исключения наследуют от `AllaError` в `src/alla/exceptions.py`
- [ ] Нет hardcoded URL, токенов, путей — всё через `Settings`

## Слои и связность

- [ ] Сервисы не импортируют httpx напрямую — работают через Protocol-интерфейсы
- [ ] Новый сервис подключён через `orchestrator.py` (не вызывается из `cli.py` или `server.py` напрямую)
- [ ] CLI (`cli.py`) и сервер (`server.py`) используют только `orchestrator.analyze_launch()`

## Документация

- [ ] `CLAUDE.md` обновлён: раздел «Что сделано», таблица конфигурации, примеры команд
- [ ] `.env.example` содержит новые переменные с комментарием

## Ручная проверка

- [ ] `alla --help` отрабатывает без ошибок (deferred imports не сломаны)
- [ ] `alla --version` отрабатывает
- [ ] `alla {launch_id}` с новой фичей отключённой работает как раньше (backward compatibility)

---

Для каждого ❌ укажи: что нарушено и в каком файле исправить.
