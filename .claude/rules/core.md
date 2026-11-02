---
paths:
  - "qwen-skill/alla-launch/scripts/alla_core/**"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/pipeline.py"
  - "qwen-skill/alla-launch/tests/test_core_*.py"
  - "qwen-skill/alla-launch/tests/test_clustering_optimizations.py"
  - "qwen-skill/alla-launch/tests/test_log_selection.py"
---

# Ядро `alla_core`

Подробности — в файлах подобластей: `core-fetch.md` (выгрузка и триаж), `core-log-events.md` (события лога), `core-logs.md` (операция теста и отбор логов), `core-clustering.md` (кластеризация), `core-client.md` (клиент TestOps и настройки).

`scripts/alla_core/` — доступ к TestOps и анализ падений, на нём стоит `alla_skill_lib`.

- Состав: клиент TestOps, триаж, логи, кластеризация, сборка блока «Данные» для заданий
  (`services/prompt_builder_service.py`). Правил и «Задания» для модели в ядре нет — они в
  `alla_skill_lib` (см. `skill-analysis-task.md`); сигнатура проблемы — тоже там
  (`signature.py`, см. `skill-memory-signature.md`).
- Происхождение: вендорено из серверного `src/alla` (коммит `e100909`), теперь обычный
  рукописный код: генератора, пометок GENERATED и drift-теста нет, правится напрямую, с
  тестами.
- Унаследованное от сервера, что скиллу может быть не нужно, удалять осознанно: сверяться с
  импортами `alla_skill_lib` и тестами. Модели базы знаний, обратной связи и LLM сервера уже
  удалены.
- TestOps читается только через его MCP-сервер (`clients/mcp_client.py`, `core-client.md`):
  REST API у пользователей закрыт. Из MCP вызывается только поиск результатов
  (`testops_find_testresults`); инструменты записи MCP (тест-кейсы, mute) не вызывать.
  Единственная запись — REST `resolve_test_result` (`clients/testops_client.py`, с обменом
  токена в `auth.py`): статус passed одного результата после перезапуска, только по «да»
  пользователя и только с `ALLURE_RERUN_RESOLVE=true` (`skill-rerun-resolve.md`). Других
  записей не добавлять без явного решения пользователя.

## Сбор данных

`TriageService` → `LogExtractionService` → `ClusteringService`, склеены в
`alla_skill_lib/pipeline.py`.

## Тесты ядра

`test_core_*.py` (кластеризация, gates по шагу, логу и ресурсам, извлечение логов и вложений,
декодирование, триаж, клиент MCP и REST-resolve, auth, `log_utils`) перенесены из серверных тестов
`e100909` с импортами `alla_core`. Тип вложения определяется по его MIME из TestOps и первым
байтам (`_detect_content_type`); `python-magic` не используется.
