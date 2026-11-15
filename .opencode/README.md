# OpenCode

Настройка OpenCode V2 для разработки скилла `alla-launch`. Не часть runtime скилла:
эти файлы нужны только инженеру и агенту, который правит репозиторий.

## Что здесь

| Путь | Что делает |
|---|---|
| `opencode.jsonc` | Serena MCP, запреты для holdout и `.env`, `git push` через подтверждение, `experimental.todo`, `watcher.ignore` |
| `plugins/alla-quality/index.ts` | Подключает `.claude/rules/` по `paths:`, гоняет ruff и docs/prompts-тесты после правки, повторяет pre-commit gate, предупреждает о holdout |

## Serena

Serena даёт symbol-level навигацию по Python: `find_symbol`, `find_referencing_symbols`,
`get_symbols_overview`, `find_declaration`, `find_implementations`. В OpenCode V2
встроенного LSP-инструмента нет, поэтому это основной способ отвечать на вопрос
«где используется этот символ».

Установка (один раз):

```bash
uv tool install -p 3.13 serena-agent
```

Проект описан в `.serena/project.yml`: `read_only: true` (субагенты не меняют код через
Serena), `ignored_paths` исключает `tests/eval/corpus_holdout.py`, `default_modes` —
`no-memories` и `no-onboarding`. Команда запуска в `opencode.jsonc` добавляет
`--agent-interface tools` (по умолчанию в Serena 2 стоит REPL), `--context ide` и
`--project-from-cwd`, чтобы работать и в worktree.

`read_only` — не жёсткая защита: Serena не подчиняется `read`-permission OpenCode.
Holdout остаётся проверочным корпусом по правилам репозитория, а не по механической
блокировке.

## Плагин `alla-quality`

Плагин читает `.claude/rules/*.md` и подключает только правила, чьи `paths:` совпали
с прочитанным или изменённым файлом. Правило добавляется в `session.instructions.entry`
один раз за сессию; все 33 файла сразу не загружаются.

После `edit`/`write`/`patch`:

- для `.py` — `ruff check --output-format concise` по изменённому файлу;
- для `SKILL.md`, `references/*.md`, `scripts/alla_skill_lib/*.py`,
  `tests/test_skill_docs.py`, `tests/test_skill_prompts.py` — `pytest -q -x` по
  docs/prompts-тестам.

Ошибки добавляются в результат инструмента, поэтому модель видит их сразу.

Перед `git commit` плагин запускает `.claude/hooks/pre_commit_check.sh` с тем же JSON,
что и Claude Code: ruff, полный pytest, mypy с baseline. `git commit --no-verify`
намеренно пропускается. Если скрипта нет, проверка не выполняется.

Проверки идут в активной папке (`ctx.location.directory`), а не в canonical checkout.
В worktree без `.venv` они пропускаются — как и в Claude Code; для полного gate
поставьте окружение в worktree.

Holdout: `read` переводится в `ask` с напоминанием про
`tests/eval/holdout_journal.md`, `edit` запрещается. Это дублирует правила из
`opencode.jsonc` и важно для субагентов: их агентские правила разрешают `read *`
и иначе перекрыли бы проектный `ask`.

Отладка:

```bash
ALLA_QUALITY_DEBUG=1 opencode run "…"
```

Плагин использует экспериментальный API `session.instructions.entry`; при обновлении
OpenCode проверить `plugin list`, `mcp list` и один сценарий с чтением файла из
`.claude/rules/`.
