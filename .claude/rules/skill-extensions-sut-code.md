---
paths:
  - "qwen-skill/alla-launch/extensions/sut-code/**"
  - "qwen-skill/alla-launch/tests/test_ext_sut_code.py"
---

# Расширения: sut-code

## sut-code

Разбор проблемы по коду тестируемого приложения по просьбе пользователя (`/alla:code N
[ревизия] [ссылка]`, хук `on_request`, субагент `alla-sut-code`). Подсказка после отчёта —
`scripts/hint.py` (`after_report`, проблемы «приложение»/«неизвестно» без `confirmed`).

- Копия — `<проект>/.alla-sut/` со своим `.gitignore` (`*`): `<имя>-<sha1(url)[:6]>/repo.git` —
  bare partial clone (`--filter=blob:none`, ветки в `refs/heads/*`), `trees/<sha12>` — `git
  worktree --detach` (+ `<sha12>.ready`), `lock` — `fcntl` на fetch/worktree. Первый клон и
  дерево ревизии — фоновая работа (`sutlib.py job`, `start_new_session`, `jobs/<имя>/done|failed`):
  скрипт ждёт её `WAIT_BUDGET` (70 с), иначе `SutPending` — «повтори через пару минут»; клон
  идёт во временную папку и переименовывается при успехе. Очистка (`cleanup_trees`,
  `keep_trees`) не трогает деревья запроса и те, чей `.ready` моложе `TREE_BUSY_SECONDS` (час):
  каждое действие отмечает дерево (`load_state`), его может читать субагент другого запроса.
  Дерева запроса нет — `load_state` просит повторить `prepare` (`switch` — `need_trees=False`,
  он создаёт дерево сам). Дерево — `worktree add` с `-c core.symlinks=false`: символическая
  ссылка из репозитория приложения (`Service.java → ../../.env`) становится файлом с текстом
  ссылки, иначе субагент своим чтением файлов и окно кода `prepare` открыли бы файл вне
  ревизии (ревью Codex); `window` к тому же не читает путь, разрешённый за папку ревизии.
  Формат распаковки — вторая строка `.ready` (`TREE_FORMAT`, `tree_ready`): дерево без неё
  распаковано до этого правила и не готово — `ensure_tree` распаковывает заново,
  `load_state` просит повторить `prepare`, `cleanup_trees` удаляет его, даже свежее и в
  пределах `keep_trees` (кроме дерева текущего запроса). Тот же формат — в `spec.json`
  фоновой работы (`Job.format`, `job_format`): работу прежней версии, пережившую таймаут,
  `ensure_tree` не подхватывает — идущую дожидается (`SutPending`), затем удаляет и
  распаковывает заново (ревью Codex). Меняя способ распаковки, меняйте `TREE_FORMAT`. `timeout = 110` в манифесте: Qwen
  обрывает команду на 120 с (`SHELL_COMMAND_TIMEOUT_MS`, исходники Qwen Code 0.25).
- Ссылки (`check_url`): https/http/ssh и `git@host:path`; без пароля в URL, без `::`, без
  ведущего «-»; локальный путь — только `allow_local` (тесты, стенд). git — с
  `protocol.ext.allow=never`, `GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`, если в процессе
  скрипта не задан `GIT_SSH_COMMAND` и в git нет `core.sshCommand`. Родительский
  `GIT_SSH_COMMAND` через `run_script` не наследуется; `SSH_AUTH_SOCK` сохраняется.
- Ревизия (`prepare.choose_revision`): `--param ref=X` (или `имя=X` для одного репозитория) →
  ветка, тег `X`/`vX`, коммит; иначе приблизительно — `rev-list -1 --before=<launch_created>`
  основной ветки (`default_branch` или HEAD bare-копии). Версию из TestOps пока не читаем
  (решение отложено).
- `prepare` пишет `<stem>.sut.md` (ревизии и папки, места кадров стека всех языков с окнами
  кода, `git grep -F` постоянных кусков текстов ошибок, куски `[S…]`) и `<stem>.sut-state` (не
  `.json`: `extensions action` принимает контекстом любой `.json` папки расширения).
- Поиск — действия `search` (`git grep -F -i`) и `files`: `grep_search`/`glob` Qwen копию не
  видят. `switch` пересобирает данные на другой ревизии, `history` — `git log -L`/по файлу от
  ревизии назад, `compare` — коммиты основной ветки после ревизии по файлу.
- `check` (`RESULT_FORMAT`, повторён в `task.md` и README расширения): место и цитаты кода — в
  файле ревизии ±3 строки, `[S…]` — в куске данных, коммиты ИСТОРИИ — в репозитории; попытки —
  `<stem>.check-attempts`, третья принимается с `format_ok = false`. Итог — `result.json`
  (вход будущего расширения дефектов), `report.md`, `finding.json` (контракт выводов, см.
  ниже).
- Тесты — `test_ext_sut_code.py` на настоящем git (репозиторий с тегом, веткой релиза,
  коммитом с NPE и исправлением после даты прогона).
