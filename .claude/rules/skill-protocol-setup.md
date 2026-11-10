---
paths:
  - "qwen-skill/alla-launch/scripts/alla_skill.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/qwen_files.py"
  - "qwen-skill/alla-launch/commands/**"
  - "qwen-skill/alla-launch/tests/test_skill_update.py"
  - "qwen-skill/alla-launch/tests/test_skill_commands.py"
  - "qwen-skill/alla-launch/scripts/alla_skill_lib/workspace.py"
---

# Протокол: установка и слэш-команды

## Точка входа и установка

- Bootstrap `alla_skill.py`, `alla_release.py`, `alla_wheels.py` — stdlib, Python 3.8+; основная логика — 3.11+.
- `setup` без флагов только проверяет готовую `.venv`, Python и точные версии всех применимых
  записей lock-файла. Нет окружения/версий — `setup_required`; сломанный или старый Python —
  `error`. Автоматические сценарии на этом останавливаются, pip не запускают.
- Только явный `setup --wheelhouse PATH [--python PATH]` создаёт venv, локально запускает
  ensurepip при необходимости и pip с `--no-index --find-links --only-binary :all:` и hashes.
  `alla_wheels.py` готовит временный find-links только из regular .whl (HTML игнорируется,
  ссылки отклоняются); --no-deps исключает URL из metadata, весь граф есть в lock.
  PIP_* очищаются, PIP_CONFIG_FILE=os.devnull. Lock допускает только точные hashed deps;
  URL/VCS/includes/options отклоняются. Произвольные pip-аргументы удалены.
  Существующий venv при сбое не удаляется и не пересоздаётся.
- Успешная проверка зависимостей атомарно пишет `.venv/.alla-setup-complete` (хеш lock),
  создаёт `.env` из образца с правами600. `.gitignore` проекта не меняется.
- `update [--source PATH]` работает без venv. `[update] source` — локальный путь; прежние
  repo/ref/path и URL запрещены. Git не вызывается. Каталог/ZIP/TAR/TAR.GZ проходят проверку
  путей, links/special files, collisions, лимиты100MiB/10000entries/256MiB распакованного потока.
  `.env` и backups, `.venv`, `.git`, кэши не входят в payload/diff; `.env.example` сохраняется.
  Источник закрепляется fd: O_NOFOLLOW/fstat, обход каталогов через dir_fd без повторного
  разрешения пути. Исключённые .env не открываются. Пользовательские symlink parents
  запрещены; root-owned системные /tmp,/var,/etc aliases допускаются только по точной карте.
  Архив читается из одного fd в ограниченный private snapshot, затем парсится только снимок.
- Релиз с sha256 по путям и contenthashes атомарно публикуется в
  `<project>/.alla-releases/<digest>/alla-launch` с manifest/complete/diff. Повтор проверяет
  все байты; повреждённый каталог не перезаписывается. `STATUS: update_ready` показывает пути.
  Активация вручную, старая папка сохранена; никакого swap активной папки или установки deps.
- CI smoke получает заранее подготовленный ALLA_WHEELHOUSE: подготовка колёс — отдельный
  явный шаг CI; внутри сценария скилла только offline установка, затем synthetic check/prepare.

## Слэш-команды `/alla:*` (`commands/alla/`, `qwen_files.py`)

- `commands/alla/*.md` — команды Qwen Code (`FileCommandLoader`: подпапка `alla/` даёт имя
  `alla:<файл>`): `run`, `continue`, `apply`, `revert`, `remember`, `reject`, `review`,
  `setup`, `update`, `extensions`, `rerun`. Frontmatter — `description`, `argument-hint` (у команд с аргументами, тогда в теле
  `{{args}}`), `disable-model-invocation: true` (без него команду может вызвать сама модель).
- Тело — короткая просьба «вызови скилл alla-launch (инструмент skill) и действуй по разделу
  N»: шаги и правила не копируются, источник — `SKILL.md`. Папку разбора для `apply`,
  `revert`, `remember`, `reject`, `review` команда не знает — правило в разделе `SKILL.md`
  «Слэш-команды» (папка из сеанса, иначе `next` без аргументов; краткий разбор повторно не
  выводить).
- Qwen читает файлы при старте сеанса. `install-files` и `extensions enable/disable`
  сначала показывают полный diff. Просмотр ничего не пишет; отказ не требует удаления
  конфигурации. После явного согласия `--yes --diff SHA256` применяет просмотренный snapshot:
  project root, пути, старые/новые байты и config.toml входят в хеш. Изменился source/target/config
  — новый diff и согласие. Хеш не является доказательством человеческого согласия.
- `file_transaction.py`: блокировка проекта, проверка snapshot под lock, durable rollbackjournal
  с правами600, запись файлов и config последним. Сбой восстанавливает старые байты, следующий
  вызов восстанавливает незавершённую транзакцию; чужие изменения останавливают recovery.
  Журнал принимает только пути файлов Qwen и config, target parents не могут быть symlinks.
  Disable удаляет только совпадающие с исходниками файлы расширения из показанного diff.
- Setup/update/prepare не ставят файлы в `.qwen/`; активные агенты/команды обновляются
  отдельным просмотром и согласием. Стенд имеет сценарии отказа I02/I03 и staged update I04.
