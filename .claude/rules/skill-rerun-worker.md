---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/rerun.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/rerun_worker.py"
  - "qwen-skill/alla-launch/tests/skill_fake_maven.py"
---

# Перезапуск: план и фоновое задание

## План (`rerun.build_plan`, `locate`)

- Тест → `code_hints.jvm_test_of` (один JVM-исходник, класс с пакетом из `full_name`, метод
  сразу после класса; `$` — «вложенный класс — пока не поддерживается»; части класса и метод —
  только идентификаторы `[A-Za-z_][A-Za-z0-9_]*`: имя — данные TestOps, а `+`, `*`, `,`, `?`
  surefire понял бы как шаблон и запустил бы больше плана — ревью Codex) → `modules.build_dir`
  (с корнем проекта) → `build_tool`: `gradle` — «Gradle пока не поддерживается», нет pom —
  «нет файла сборки». Причина попадает в «Запустить нельзя».
- Группа на модуль (`MavenGroup`): корень реактора — `modules.maven_reactor`; модуль не
  корень реактора — `-pl <модуль> -am`, запасной `fallback_cwd` — папка модуля. Инструмент —
  только `mvn` из PATH (тот же executable для attempts/fallback, wrapper запрещён),
  иначе `ok` «Maven не найден». Windows — не поддерживается (killpg, `.cmd`).
- `changed` («правка агента применена» — есть `proposals/NN.applied.json`; «код теста
  изменён» — `git status --porcelain` исходника): прошедший тест — «проверка правки», в
  нестабильные не идёт. Исходник меняли уже во время задания (`source_sha` плана против
  `source_digest` на момент успеха) — тоже: `changed` = «код теста изменён во время
  перезапуска» (ревью Codex).
- Оценка времени — сумма `duration_ms` плюс `BUILD_OVERHEAD_S` на модуль, на число запусков.
- Лимит `ALLURE_RERUN_MAX_TESTS` проверяется до `git status`, а `git status` — один раз на
  исходник (`_changed`, кэш `dirty`): план строится на каждом `done`, кандидатов бывают сотни
  (ревью Codex: 500 вызовов git вместо 20).

## Фоновое задание (`rerun_worker`)

- Логи — `rerun/logs/attempt-<k>-<модуль>-<sha1(модуль)[:6]>.log`: без хэша `a/b` и `a_b`
  писали в один файл, и причина сборки одного модуля попадала к другому (ревью Codex).
- `start_job`: `rerun/plan.json` (тесты, группы, `maven_args`, путь блокировки), процесс
  `python -m alla_skill_lib.rerun_worker <rerun/>` из `scripts/`, новая сессия, stdin
  `DEVNULL`, вывод в `worker.log` (иначе Qwen ждал бы конца вывода), `pid` с временем. Новое
  задание чистит файлы прежнего — под `rerun/start.lock` и только если прежнее не идёт:
  второй одновременный `--yes` получает `running` «уже идёт» (ревью Codex: он затирал план
  идущего, и итог первого не совпадал с `job`). Окружение (`worker_env`) без `ALLURE_*` (токен),
  `QWEN_CODE_*` и `PYTHONPATH`; значения `ALLURE_RERUN_MAVEN_ARGS` не печатаются ни в плане,
  ни в логе (`<доп. аргументы>`).
- Один перезапуск на проект: воркер держит `fcntl`-блокировку
  `<проект>/alla-reports/.rerun.lock` (`project_lock` — папка отчётов по умолчанию, с
  `.gitignore`, даже при своём `--reports-dir`: разборы одного проекта с разными папками
  иначе гоняли Maven одновременно — ревью Codex); `--yes` проверяет её заранее (`lock_busy`). Состояние (`job_state`): `result.json` того же
  `job` — done; `failed` — причина; живой `pid` (не старше потолка + `STALE_MARGIN_S`, зомби
  своей команды — `waitpid`) — running; иначе «прервался».
- Команда Maven: `-o -B test -Dtest=A#m1+m2,B#m3 -Dsurefire.failIfNoSpecifiedTests=false
  -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -DskipTests=false
  -Dmaven.test.skip=false -Dsurefire.reportNameSuffix=alla-<job>-<k>`
  + `shlex.split(ALLURE_RERUN_MAVEN_ARGS)`. Без `-ntp` (Maven < 3.6.1). `skipTests=false` и
  `maven.test.skip=false` — против пропуска в `.mvn/maven.config` (2026-10-27, у пользователя
  «Tests are skipped» и «не запустился»): `MavenCli.cliMerge` (3.8.x, 3.9.x) добавляет `-D`
  командной строки после опций `maven.config`, они и действуют. Пропуск в pom (`<skipTests>`
  в настройке surefire) так не отменить: отчётов нет, код 0, в логе `TESTS_SKIPPED` —
  причина `SKIPPED_BY_BUILD_REASON` («Maven пропустил тесты…», совет про
  `ALLURE_RERUN_MAVEN_ARGS`) вместо «нет в отчётах surefire» (`rerun_worker._missing_reason`).
  Фейковый `mvn` читает ближайший `.mvn/maven.config` с тем же порядком, `pom_skip` — пропуск
  из pom. На настоящем Maven (ревью Codex, `-o`, surefire 3.2.5): `maven.config` с
  `-DskipTests` и `-Dmaven.test.skip=true` — тест выполнен; `<skipTests>true` в pom — «Tests
  are skipped.», код 0. Ненулевой код —
  сборка не прошла (`build_errors`: до 5 строк `[ERROR]` по 300 символов, без похожих на
  секрет). В логе «Could not find the selected project in the reactor» — повтор из папки
  модуля.
- Отчёты: `target/surefire-reports/TEST-*-<суффикс>.xml` (настоящий surefire 3.2.5:
  `classname="Класс(<суффикс>)"`, параметры — `m(int)[1]`); суффиксных нет (pom задаёт свой)
  — `TEST-*.xml`, которых до запуска не было или которые запуск переписал (`report_snapshot`:
  имя → `st_mtime_ns`; допуск «не старше начала» брал отчёт прошлого запуска секундной
  давности за итог сломанной сборки — ревью Codex), и не от прежних перезапусков
  (`-alla-…-N.xml`).
  `failure`/`error`/`rerunFailure`/`rerunError` — упал; `flakyFailure`/`flakyError` без них —
  прошёл, `surefire_flaky`; `skipped`. Метод прошёл, только если прошли все вызовы; хоть
  один пропущен — `skipped` (ревью Codex: упавший в CI вариант мог не выполниться, а тест
  уходил в нестабильные и в базу знаний).
- Попытки: в следующий запуск идут только упавшие с той же ошибкой, что в TestOps
  (`same_error`: `text_normalization.message_key` — сообщение целиком, до `MESSAGE_KEY_LINES` строк, пробелы
  схлопнуты, через `repeat_key`; ошибки в TestOps нет — «та же»). Не первая строка: у
  RestAssured она общая («1 expectation failed.»), а коды ниже — 401 сходил за 500 в CI и
  снимал отметку (ревью Codex). Сообщение из отчёта — атрибут `message`, иначе первая строка
  текста без класса исключения. Вызовы параметризованного метода упали по-разному — берётся
  тот, что совпал с CI, а не первый в XML (ревью Codex). Итоги:
  `passed` (запуск k), `reproduced` (все N), `other` (другая ошибка — без повторов: локальная
  среда, а не нестабильность), `not_run` (сборка, нет в отчётах, skipped, время). Тест, уже
  упавший той же ошибкой, а дальше не проверенный (время, сборка, skipped), — `reproduced` с
  `cut` = причина (ревью Codex: он становился «не запустился»); такой в базе знаний отметку
  не теряет. Потолок `ALLURE_RERUN_TIMEOUT` — общий; по нему `killpg` SIGTERM, ждём, пока
  опустеет вся группа (не только Maven: форк surefire может игнорировать SIGTERM), через
  `KILL_GRACE_S` — SIGKILL группе.

## Offline и ограничение команды

Каждая команда — `mvn -o -B ... test -Dtest=<точные методы>`. Пустой/шаблонный selector
отклоняется на границе запуска. Дополнительные аргументы — только безопасные -P/-D;
цели/POM/settings/reactor и test/it.test/skipTests/skipITs/surefire.*/failsafe.*/maven.* запрещены.
Перед каждым запуском проверяется применимый `.mvn/maven.config`; конфликт — not_run.
Поиск останавливается на ближайшем каталоге `.mvn`, даже без config. Грамматика config —
одна whitespace-free опция на строку, без кавычек/обратной косой черты/управляющих символов;
LF/CRLF и full-line # comments поддерживаются. Shlex для config запрещён: его экранирование
и inline comments скрывают отдельные нативные опции Maven (например --settings). Фейк
моделирует разбиение Maven3.9+ отдельно от production parser, чтобы не маскировать обход.
MAVEN_ARGS/OPTS/BASEDIR/PROJECTBASEDIR/CONFIG, JVM injection и credentials моделей/сервисов
удаляются из окружения. MAVEN_SKIP_RC всегда устанавливается в 1: startup rc не может
вернуть MAVEN_ARGS после очистки. Нет кэша зависимостей — not_run без online retry; уже наблюдённый
результат теста сохраняется. Offline Maven не ограничивает сеть самого автотеста.
