---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/kb.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/modules.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/feedback.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/history.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/signature.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/flaky.py"
  - "qwen-skill/alla-launch/references/feedback.md"
  - "qwen-skill/alla-launch/tests/test_skill_memory.py"
---

# Память скилла: база знаний, модули, обратная связь, история

Подробности — в файлах подобластей: `skill-memory-kb-auto.md` (разбор без модели), `skill-memory-signature.md` (сигнатура), `skill-memory-modules.md` (модули), `skill-memory-feedback.md` (обратная связь и история).

База знаний проектная: `alla-kb/` в репозитории автотестов, наполняется обратной связью
пользователя.

## База знаний (`kb`)

- Где лежит: `alla-kb/<id>.json` в корне git; в многомодульном Java/Kotlin-проекте
  (Gradle/Maven) — `<модуль>/alla-kb/` (`kb_dir_for`, см. `skill-memory-modules.md`, «Модули»).
- Узнавание: точно по сигнатуре `v<версия>:<hash>` (`confirmed_signatures` /
  `rejected_signatures`) или по короткому признаку `error_example` — все его строки есть в
  данных кластера, без учёта чисел/ID/времени/e-mail; хранится нормализованным.
- Правило «запись подходит кластеру» одно — `record_matches` (отвергнута — `record_rejected`
  → нет; подтверждённая сигнатура → `exact`; тест кластера в `confirmed_tests` и признак
  совпал → `exact`; все строки признака в данных → `fingerprint`): им пользуются
  `match_cluster` в `prepare` и проверка известных проблем при сборке отчёта
  (`known_issues`, см. `skill-analysis-known.md`). Тесты кластера — `cluster_tests` в `prepare`,
  `entry_tests` по `run.json` потом.
- **Смена версии сигнатуры.** Тесты записи действуют, только когда у неё есть сигнатуры
  того же вида и все — прежних версий (`_migrating`): подтверждение — тест в
  `confirmed_tests`, отказ — тест в `rejected_tests`; в обоих случаях нужен совпавший
  признак (тест падает и по другим причинам). Пустой список сигнатур — не смена версии
  (последнюю переназначили другой записи или правили руками). Подтверждённая сигнатура
  кластера сильнее старого отказа по тесту — внутри `record_rejected`, чтобы `prepare` и
  отчёт (`known_issues` спрашивает отказ отдельно) решали одинаково (ревью Codex).
  `prepare` в `alla-kb/` не пишет: новая сигнатура попадёт в запись со следующим
  `remember`. Поэтому версию можно поднимать: точное узнавание и отказы переживают её
  у записей с тестами (записи до 2026-10-18 тестов не имеют — только признак).
- Тесты записи — `confirmed_tests`/`rejected_tests` (`key_of_test`: `full_name` без `[...]`
  параметров в конце, иначе `testcase:<testCaseId>`; имя, похожее на секрет (`secret_lines`),
  не сохраняется — ревью Codex): `remember` и `reject` пишут тесты кластера
  (`entry_tests` по `run.json`, `prepare` — `cluster_tests` по моделям) зеркально
  сигнатурам — `confirm` снимает тесты с отказа, `reject` с подтверждения; у других записей
  тесты не трогаются (тест падает и по нескольким известным причинам), кроме записи, которая
  уже точно узнаёт эту ошибку (`record_matches` → `exact`: по сигнатуре или, после смены
  версии, по тесту): `remember` новой записи отвечает «уже подтверждали в записи …», а
  `remember --entry` другой записи снимает с прежней сигнатуру и тесты кластера (ревью Codex). Не больше
  `MAX_RECORD_TESTS` (50), новые важнее старых. Зачем — узнавание после смены версии
  сигнатуры.
- Свои поля записи (`jira`, `owner`…) сохраняются при перезаписи. Запись атомарная, LF.
- Сканер секретов отклоняет пароли, токены, ключи AWS/GitHub, пароль в URL, PEM.

### Отметки «нестабильный тест» (`flaky.py`, `skill-rerun-candidates.md`)

- `<kb_dir>/flaky/<Класс>_<метод>_<sha1(ключ)[:10]>.json` — одна отметка на тест (общий
  `flaky.json` в `alla-kb/` читался бы как запись без `id`: `ProjectKB.load` берёт `*.json` без
  подпапок, `discover_kb_dirs` в `alla-kb` не спускается). Поля: `test` (`key_of_test`),
  `since`, `seen`, `launch_id`, `passed_on`, `attempts`, `signals`; текста ошибки нет (нечего
  проверять сканером секретов). Свои поля файла сохраняются; битый файл пропускается.
- Пишет только `rerun --remember --result <job>` после «да» (`flaky.remember_plan`): отметить
  — прошёл, код как в CI (`changed` пуст) и был признак из CI (`signals` плана: повтор,
  временный сбой, прежняя отметка); снять — отмеченный тест упал все `attempts` раз той же
  ошибкой (не `cut`). Без признака из CI не отмечается: стенд мог лежать в CI и подняться.
- Читают при сборке отчёта и отборе (`marks_for_run`: база модуля каждой проблемы, как
  `known_issues`). В `run.json` не пишется. Как отметка влияет на отбор и отчёт —
  `skill-rerun-candidates.md`, «База знаний (`rerun --remember`)».
- `README.md` базы (`kb.README`) говорит про `flaky/`; создаётся и первой отметкой.
