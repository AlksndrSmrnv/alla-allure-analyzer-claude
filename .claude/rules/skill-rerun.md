---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/rerun.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/rerun_worker.py"
  - "qwen-skill/alla-launch/commands/alla/rerun.md"
  - "qwen-skill/alla-launch/tests/test_skill_rerun.py"
  - "qwen-skill/alla-launch/tests/skill_fake_maven.py"
---

# Перезапуск тестов локально (`rerun`)

Решения пользователя (2026-10-24, план `rosy-wishing-walrus`): кандидатов предлагает скилл,
плюс `/alla:rerun N`; только Maven (JUnit/TestNG через surefire); до 3 запусков до первого
успеха; итог — в отчёт и, после «да», в базу знаний.

## Команда и статусы (`cli.cmd_rerun`)

- `rerun N [N…] --run DIR` — план (`STATUS: rerun`), ничего не запускает. Тесты — все
  участники проблем (`rerun.failed_tests_of`: представитель первым), параметры одного
  метода — один запуск. Готовая команда — `--yes --plan <хэш>`: `Plan.digest` от тестов
  (id, класс, метод, модуль) и числа запусков; другой хэш или без `--plan` — снова
  `rerun` с планом, ничего не запущено (как `apply --diff`).
- `--yes` запускает фоновое задание и ждёт итога до `WAIT_BUDGET` (75 с от начала команды:
  Qwen обрывает команду через 120 с). Не дождался — `running` с командой `--wait`;
  `count_wait` считает ответы «идёт» по заданию, после `MAX_WAITS` (4) ответ велит сказать
  пользователю, что перезапуск идёт в фоне, и больше не ждать.
- `--wait` — ход или итог (`rerun_done`, итог между `===ПЕРЕЗАПУСК===` и `===КОНЕЦ===`, не
  `===ОТЧЁТ===`: на нём держатся `report_verbatim` и `review`); перезапуска не было — `ok`.
  Пока задание идёт, новый план не строится (`running`).
- `ok`, а не `error`: перезапускать нечего (не Maven, Gradle, нет pom, нет Maven), выключено
  (`ALLURE_RERUN_MAX_TESTS=0`), занято другим перезапуском проекта, фоновый процесс упал.
  `error` (код 1, `review` считает сбоем) — только неверные номера, папка разбора и `.env`.
- Настройки — `Settings.load(require_testops=False)`: адрес и токен TestOps не нужны.

## План (`rerun.build_plan`, `locate`)

- Тест → `code_hints.jvm_test_of` (один JVM-исходник, класс с пакетом из `full_name`, метод
  сразу после класса; `$` — «вложенный класс — пока не поддерживается») → `modules.build_dir`
  (с корнем проекта) → `build_tool`: `gradle` — «Gradle пока не поддерживается», нет pom —
  «нет файла сборки». Причина попадает в «Запустить нельзя».
- Группа на модуль (`MavenGroup`): корень реактора — `modules.maven_reactor`; модуль не
  корень реактора — `-pl <модуль> -am`, запасной `fallback_cwd` — папка модуля. Инструмент —
  `mvnw` реактора, затем корня проекта (без права запуска — `sh mvnw`), иначе `mvn` из PATH,
  иначе `ok` «Maven не найден». Windows — не поддерживается (killpg, `.cmd`).
- `changed` («правка агента применена» — есть `proposals/NN.applied.json`; «код теста
  изменён» — `git status --porcelain` исходника): прошедший тест — «проверка правки», в
  нестабильные не идёт.
- Оценка времени — сумма `duration_ms` плюс `BUILD_OVERHEAD_S` на модуль, на число запусков.

## Фоновое задание (`rerun_worker`)

- `start_job`: `rerun/plan.json` (тесты, группы, `maven_args`, путь блокировки), процесс
  `python -m alla_skill_lib.rerun_worker <rerun/>` из `scripts/`, новая сессия, stdin
  `DEVNULL`, вывод в `worker.log` (иначе Qwen ждал бы конца вывода), `pid` с временем. Новое
  задание чистит файлы прежнего. Окружение (`worker_env`) без `ALLURE_*` (токен),
  `QWEN_CODE_*` и `PYTHONPATH`; значения `ALLURE_RERUN_MAVEN_ARGS` не печатаются ни в плане,
  ни в логе (`<доп. аргументы>`).
- Один перезапуск на проект: воркер держит `fcntl`-блокировку `<папка отчётов>/.rerun.lock`;
  `--yes` проверяет её заранее (`lock_busy`). Состояние (`job_state`): `result.json` того же
  `job` — done; `failed` — причина; живой `pid` (не старше потолка + `STALE_MARGIN_S`, зомби
  своей команды — `waitpid`) — running; иначе «прервался».
- Команда Maven: `-B test -Dtest=A#m1+m2,B#m3 -Dsurefire.failIfNoSpecifiedTests=false
  -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dsurefire.reportNameSuffix=alla-<job>-<k>`
  + `shlex.split(ALLURE_RERUN_MAVEN_ARGS)`. Без `-ntp` (Maven < 3.6.1). Ненулевой код —
  сборка не прошла (`build_errors`: до 5 строк `[ERROR]` по 300 символов, без похожих на
  секрет). В логе «Could not find the selected project in the reactor» — повтор из папки
  модуля.
- Отчёты: `target/surefire-reports/TEST-*-<суффикс>.xml` (настоящий surefire 3.2.5:
  `classname="Класс(<суффикс>)"`, параметры — `m(int)[1]`); суффиксных нет (pom задаёт свой)
  — `TEST-*.xml` не старше начала запуска и не от прежних перезапусков (`-alla-…-N.xml`).
  `failure`/`error`/`rerunFailure`/`rerunError` — упал; `flakyFailure`/`flakyError` без них —
  прошёл, `surefire_flaky`; `skipped`. Метод прошёл, только если прошли все вызовы.
- Попытки: в следующий запуск идут только упавшие с той же ошибкой, что в TestOps
  (`same_error`: `repeat_key` первых строк; ошибки в TestOps нет — «та же»). Итоги:
  `passed` (запуск k), `reproduced` (все N; `cut` — вышло время), `other` (другая ошибка —
  без повторов: локальная среда, а не нестабильность), `not_run` (сборка, нет в отчётах,
  skipped, время). Потолок `ALLURE_RERUN_TIMEOUT` — общий; по нему `killpg` SIGTERM, через
  `KILL_GRACE_S` — SIGKILL.

## Проверка

- `tests/test_skill_rerun.py` — фейковый `mvnw` (`skill_fake_maven.py`: сценарий итогов по
  `класс#метод`, `-pl`, суффикс, «нет в реакторе», сломанная сборка, пауза).
- Настоящий Maven вручную (2026-10-24, Maven 3.10, JDK 27, JUnit 5.11, surefire 3.2.5, `-o`):
  нестабильный тест прошёл на запуске 2 из 3, сломанный — 3 из 3, в третий запуск ушёл
  только он. В CI Maven не гоняется.
