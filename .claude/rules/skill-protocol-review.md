---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/review.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/review_model.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/session_log.py"
  - "qwen-skill/alla-launch/tests/test_skill_review.py"
  - "qwen-skill/alla-launch/tests/test_skill_session_log.py"
  - "qwen-skill/alla-launch/tests/qwen_stand.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/session_rules.py"
---

# Протокол: проверка разбора для пилота

Сценарий стенда `I06` проверяет инструкцию подключения MCP трекера при доступе только через
токен окружения Qwen: агент читает `CONNECT-MCP.md` и останавливает подключение без правок,
не предлагая восстановить токен через `adapter_env` или изменить фильтр окружения.

Сценарии установки `I01`–`I03`, `I05` проверяют инструкцию инженеру без установки:
`setup` не создаёт окружение, `install-files` и enable/disable не меняют файлы и config,
в том числе после обычного «Да». `installation_unchanged` сравнивает снимок файлов установки;
`no_dependency_install` проверяет команды и отсутствие нового venv. `P02` — остановка
при неготовом окружении, `A04` — отрицательный запрос активации.

Стенд не пишет `ALLURE_TOKEN` в `.env`: обычные готовые сценарии получают синтетический токен
через окружение процесса, `env_ready=False` — без токена. `S01` проверяет зелёный прогон 778
с native credential store Qwen 0.27.0: до старта модели настоящий CLI устанавливает
`alla-launch-credentials` и сохраняет токен в отдельном HOME через PTY. Общая utility —
`tests/qwen_credentials_support.py`, без pytest; `QWEN_CODE_FORCE_FILE_STORAGE=true`
исключает личный keychain. Агенту токен через окружение не передаётся. Проверяются
завершение, SAFE, отсутствие утечек и plaintext `.env`, права `0600` на encrypted file.
Отсутствуют qwen/node — `not_run`; несовместимая версия или ошибка native установки —
ошибка стенда, а не пропуск. `asks_for_env` требует адрес в `.env`, токен в хранилище Qwen.

## Проверка разбора для пилота (`review`, `review.py`, `review_model.py`)

- Выключена по умолчанию и на разбор не влияет: ни задания, ни ответы `next`, ни правила
  исполнителя о ней не говорят. В `SKILL.md` — только раздел 6 «по прямой просьбе»; из
  терминала пилот запускает её без модели разбора. Новый справочник для неё не заводить:
  `test_skill_docs` требует ссылку на каждый справочник из `SKILL.md`, описание — в
  `setup.md` («Пилот: проверка разбора»).
- Журнал сеанса: Qwen Code ≥ 0.25 пишет `<QWEN_HOME|~/.qwen>/projects/<sanitizeCwd>/chats/
  <сеанс>.jsonl` и `subagents/<сеанс>/agent-*.jsonl`, shell-командам передаёт
  `QWEN_CODE_SESSION_ID`/`QWEN_CODE_PROJECT_DIR`. `prepare` (новый и продолжение) и `next`
  пишут их в `state.json` (`sessions`, до 20; `qwen_project_dir`) — `session_log.note_session`.
  `review` сеанс не запоминает. Окно разбора — ходы разговора (от реплики пользователя до
  следующей, по каждому сеансу; записи субагентов — в ход своего сеанса по времени), где есть
  команда скилла с папкой разбора в выводе или в аргументах (`next --run DIR`, упавший без
  вывода папки); ход берётся целиком (упавший `next` и действия после него — тоже),
  посторонние ходы между сеансами не входят. Ход обрезается на первом `review`: проверка и
  всё после неё — не разбор (идущий `review` ещё без результата выглядел бы падением без
  STATUS). Время — сумма длительностей ходов до последней записи, включая время результата
  инструмента. Финальный ответ — текст после последнего `done` до следующего вызова; чтение и запись
  `skill-problems.md` и чтение справочников `references/*.md` его не обрывают (`done` велит
  дописать файл по `problem-report.md` до вывода отчёта). У shell в журнале вывод обёрнут `Command:`/`Output:` —
  берётся `resultDisplay.output`, иначе вырезается из `Output:`.
- Проблемы скилла для `review` — пункты `skill-problems.md` целиком, с полями «Где:»,
  «Произошло:»…; новый пункт — только следующий номер на отступе внешней нумерации, «1. …»
  из цитаты вывода — продолжение пункта (`read_skill_problems`,
  `process.skill_problems`, читаются и без журнала) плюс строки субагентов из журнала
  (`journal.skill_problems`); сумма — `review.skill_problems`.
- Правила поведения (`shell_only_skill_commands`, `no_code_search`, `listed_code_only`,
  `allowed_reads`, `allowed_writes`, `report_verbatim` — дословно и без блока «Проблемы
  скилла» в ответе) — одна реализация в
  `session_rules.py` для стенда и `review`; стенд оборачивает их под прежними именами.
  Меняя правило, проверь оба: `test_qwen_stand.py` и `test_skill_session_log.py`.
  `allowed_reads` пропускает `read_file` сохранённого вывода инструмента своего проекта —
  `<QWEN_HOME|~/.qwen>/tmp/<sha256 корня>/tool-results/` (`qwen_tool_results`): Qwen Code 0.26
  кладёт туда вывод длиннее 28 тыс. символов, в том числе тело `SKILL.md` от `skill`, и
  отдаёт модели первые 2 тыс.; `review` берёт `QWEN_HOME` из окружения (`session_log.qwen_home`).
- Заполнение окна — `session_log.ContextUse`: пик `promptTokenCount` (максимум запроса, не
  сумма) и число записей `system`/`chat_compression` (Qwen Code 0.26), отдельно у основного
  агента и у субагентов (пик — наибольший). В окне разбора — `SessionLog.context`, по всем
  журналам папки проекта Qwen — `project_context` (стенд). В `report.md` («Затраты»),
  `pilot-summary.md` («пик контекста», «сжатий истории») и краткий итог (строка только при
  сжатиях). Оценку хода не меняет: окно модели у пилота своё, порога в скрипте нет.
- Разбор по записи базы знаний (`kb_auto` в записи кластера и хэш файла в `state.json`,
  `known_issues.kb_auto_written`; так же разбор по проверенной правке — `fix_auto` и
  `fix_facts.fix_auto_written`, ревью Codex) `review` считает работой скрипта, как `auto`: не оценивает
  моделью, не считает в качестве; в размере — «из них по базе знаний — K».
- Оценка хода — коды причин `review.REASONS` (`sources_lost` — потерян реестр источников); `сбой` — `not_done`, `status_error`,
  `exception` (в выводе команды traceback), `no_status` (ни STATUS, ни traceback: тайм-аут
  инструмента, прерывание, пустой вывод — стектрейса там нет); `no_journal` и `fix_attempts` оценку не снижают. Пороги качества —
  `GOOD_CONFIRMED_SHARE`, `GOOD_MAX_CATEGORY_DISAGREE`, `FAIR_CONFIRMED_SHARE`; доли — от числа
  запрошенных проблем (невозвращённая оценка — не подтверждение), «хорошо» — только при полном
  ответе. Меняя пороги или причины, правь таблицу в `setup.md`.
- Проверяющая модель — отдельный процесс `qwen` (`ALLA_REVIEW_QWEN` или PATH): временная
  рабочая папка (в ней только копии скриншотов), `--approval-mode plan`, `--json-schema`, `--system-prompt`, окружение — унаследованное CLI через
  `child_env.inherited_env`, без ALLURE_*, QWEN_CODE_*, git/tracker tokens, SSHagent/askpass
  и Python/Node/loader injection. Экспортированные modelkeys сохраняются. Обёртка не читает
  settings.json и не обнаруживает envKey; нативную авторизацию выбирает сам Qwen CLI.
  Стенд тоже не читает личные настройки: --model/OPENAI_MODEL, --base-url/OPENAI_BASE_URL,
  --api-key-env (default OPENAI_API_KEY), отдельный HOME и QWEN_HOME. Generated settings
  содержит только имя envKey, не значение. Запрещённый sandbox read — синтетический файл.
  Таблица категорий берётся из
  `references/analysis-format.md` (`categories_table`), а не копией. Оцениваются
  `MAX_REVIEWED` (10) крупнейших по тестам проблем, разобранных моделью; оценки
  незапрошенных номеров отбрасываются. Нет `qwen`, тайм-аут (код 55), мусор — отчёт без
  оценки с `error_code`.
- Скриншоты проверяющему — без инструментов: `stage_images` копирует во временную рабочую
  папку `qwen` только файлы из `images` записи кластера оцениваемых проблем, лежащие в
  `images/<file_id>/` и изображения по байтам, под именами `pNN-S<k>.<ext>`; раздел
  `SHOTS_HEADER` проблемы ссылается на них `@имя`, и headless `qwen` сам подставляет файлы
  частями-изображениями в конец запроса (`@` разворачивается только для файлов из рабочей
  папки и временной папки `~/.qwen/tmp`, вызовов инструментов не тратит). Непринятая копия —
  «изображение не приложено»: не картинка, а также сбой чтения оригинала или записи копии
  (`_copy_image`, неполная копия удаляется) — `review` из-за скриншота не падает. `SYSTEM_PROMPT`: цитату со скриншота сверять с картинкой, без
  картинки — не засчитывать ни за, ни против, а причину только по скриншоту — «частично»
  (описано в `setup.md`). Имя копии — без пробелов и точки в конце: иначе разбор `@`
  в Qwen обрежет путь.
- `pilot-summary.md` — только числа, коды причин, имена проверок и фиксированные слова; имя
  модели и версия проходят через `_safe_word`, причина без оценки — только `error_code`.
  Новое поле в сводке — только из этих источников; утечку охраняет
  `test_pilot_summary_has_no_run_data`.
