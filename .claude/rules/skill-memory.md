---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/kb.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/modules.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/feedback.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/history.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/signature.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/flaky.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/fix_facts.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/learn.py"
  - "qwen-skill/alla-launch/references/feedback.md"
  - "qwen-skill/alla-launch/tests/test_skill_memory.py"
---

# Память скилла: база знаний, модули, обратная связь, история

Подробности — в файлах подобластей: `skill-memory-kb-auto.md` (разбор без модели), `skill-memory-signature.md` (сигнатура), `skill-memory-modules.md` (модули), `skill-memory-feedback.md` (обратная связь и история).

База знаний проектная: `alla-kb/` в репозитории автотестов, наполняется обратной связью
пользователя и проверенными фактами разбора без вопроса (отметки перезапуска и проверенные
правки, `learn.py`).

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
- Пишет сам скрипт по итогу перезапуска, без вопроса (`learn.py`, с 2026-11-05; раньше —
  `rerun --remember` после «да»), по `flaky.remember_plan`: отметить
  — прошёл, код как в CI (`changed` пуст) и был признак из CI (`signals` плана: повтор,
  временный сбой, прежняя отметка); снять — отмеченный тест упал все `attempts` раз той же
  ошибкой (не `cut`). Без признака из CI не отмечается: стенд мог лежать в CI и подняться.
- Читают при сборке отчёта и отборе (`marks_for_run`: база модуля каждой проблемы, как
  `known_issues`). В `run.json` не пишется. Как отметка влияет на отбор и отчёт —
  `skill-rerun-candidates.md`, «База знаний (без вопроса, `learn.py`)».
- `README.md` базы (`kb.README`) говорит про `flaky/` и `fixes/`; создаётся и первой отметкой.

### Проверенные правки (`fix_facts.py`, `<kb_dir>/fixes/`)

Решение пользователя (2026-11-05): правку, на которую инженер сказал «да» и после которой
тесты проблемы прошли с первого перезапуска, скилл запоминает сам. Второе мнение (Codex,
Grok): это факт эксперимента, а не подтверждённая причина, поэтому **не `KBRecord`**:
`record_matches`, `confirm()`, `confirmed_*`, `accepted_feedback` его не видят.

- Пишет `learn.learn_from_rerun` → `fix_facts.record_checked`, все условия сразу: у проблемы
  предложение `fix`, `applied_state == applied`; каждый упавший тест проблемы
  (`rerun.failed_tests_of`, с вариантами `other_ids`) в итоге — `passed`, `attempt == 1`, без
  `surefire_flaky`, `changed == "правка агента применена"`; правка не растит таймаут
  (`proposals.timeouts_increased`; смену ожидаемого значения инженер видел «Проверь:» и
  принял — она запоминается); `secret_lines` по БЫЛО/СТАЛО/файлу пуст; есть `signature`,
  `log_digest`; ключей тестов не больше `MAX_RECORD_TESTS`. Файл
  `<имя файла>_<строка>_<id>.json`, `id` — sha1(сигнатура|файл|строка)[:12]: поля
  `signature`, `log_digest`, `tests`, `file`, `line`, `before`, `after`, `launch_id`, `date` —
  без текста разбора модели.
- Узнаёт `prepare` (`fix_facts.prepared`, только если нет `kb_auto`): та же сигнатура **и**
  отпечаток лога, все тесты кластера (`cluster_tests`) — из `tests` факта, факт не старше
  `MAX_AGE_DAYS` (14), нет записи с `origin == "exact"` (слова инженера важнее), место правки
  однозначно (`proposals.location_state`: `applied` — СТАЛО в коде, `pending` — БЫЛО; иначе
  факт не подходит), цитата — `known_issues.first_quote`, текст проходит
  `check_entry_analysis`. Итог — `entry["fix_auto"]` (`id`, `state`, `file`, `line`, `date`,
  `launch_id`, `quote`); задание строится как обычно — запасной путь.
- `next` (`fix_facts.sync`, сразу после `sync_known_auto`, до подсчёта неразобранных) пишет
  `analyses/NN.md` (`analysis_text`: категория «тест», `КОД:` — место правки; первое
  предложение — «…проверенная правка fix:<id> (<файл>:<строка>, перезапуск <дата>)»: оно в
  кратком разборе, и id для `reject` модель берёт оттуда — ревью исполнителя) и `proposals/NN.md` (`proposal_text`); хэши — `state.json`
  `fix_auto`. Чужой файл (хэш другой) не трогается. Дальше обычный путь: `_proposal_step`
  принимает готовое предложение (`applied` — `apply` не предлагается, `pending` — обычный
  вопрос `apply`), `_kb_offer` такие номера не спрашивает, `_rerun_offer` их не берёт (есть
  правка). Факт удалён — свои файлы удаляются (предложение — если нет отметки `apply`), проблему
  разбирает модель.
- `reject N fix:<id>` (`feedback._forget_fix`) удаляет факт и просит `next`.
- Ограничение: правка в коде, а CI всё ещё падает так же после её мержа — скрипт 14 дней
  отвечает «дождитесь CI»; выход — «не то» → `reject N fix:<id>`.
