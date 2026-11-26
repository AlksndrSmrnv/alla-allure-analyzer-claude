#!/usr/bin/env bash
# Инженер вручную готовит синтетический проект и зависимости; команды расширения
# setup и extensions enable/disable только печатают проверку и инструкции.
# Нужны python3 (3.11+), git и заранее подготовленный ALLA_WHEELHOUSE.
# Установка зависимостей ниже — явный шаг инженера в CI, вне команд расширения и без сети.
# ALLURE_* сбрасываются, чтобы check/prepare работали только с фейковым TestOps.
set -euo pipefail

while IFS= read -r var; do unset "$var"; done < <(compgen -e | grep '^ALLURE_' || true)

repo=$(cd "$(dirname "$0")/../.." && pwd)
: "${ALLA_WHEELHOUSE:?Укажи заранее подготовленный локальный wheelhouse}"
wheelhouse=$(cd "$ALLA_WHEELHOUSE" && pwd)
work=$(mktemp -d)
project="$work/project"
ext="$work/extension"
port=8777
server_pid=""
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Ждём строку статуса в выводе команды; вывод печатается целиком для журнала CI.
expect_status() {
  local expected=$1 log=$2
  grep -qx "STATUS: $expected" "$log" || fail "ожидался STATUS: $expected"
}

mkdir -p "$project" "$ext"
for item in qwen-extension.json skills commands agents scripts extensions requirements.txt requirements.in .env.example; do
  cp -R "$repo/$item" "$ext/$item"
done
find "$ext" -name __pycache__ -type d -prune -exec rm -rf {} +
git -C "$project" init -q
entry="$ext/scripts/alla_skill.py"
cd "$project"

# Снимок содержимого, структуры и прав: в том числе игнорируемые .env/.qwen/alla.env, кэши и старый маркер.
# Внутренности установленного venv не обходятся, кроме маркера, которым раньше управлял setup.
cat > "$work/snapshot.py" <<'SNAPSHOT'
import hashlib, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
paths = [root / name for name in (".qwen", "alla-extensions", ".gitignore", ".venv", ".venv/.alla-setup-complete")]
for directory in (root / ".qwen", root / "alla-extensions"):
    if directory.is_dir():
        paths.extend(directory.rglob("*"))
result = {}
for path in sorted(set(paths)):
    if not path.exists() and not path.is_symlink():
        continue
    row = {"mode": path.lstat().st_mode}
    if path.is_symlink():
        row["link"] = os.readlink(path)
    elif path.is_file():
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result[str(path.relative_to(root))] = row
print(json.dumps(result, sort_keys=True))
SNAPSHOT
snapshot() { python3 "$work/snapshot.py" "$project"; }
unchanged() {
  snapshot > "$work/after.json"
  cmp "$work/before.json" "$work/after.json" || fail "$* изменил файлы установки"
}

echo "== без окружения check/setup только сообщают инструкцию"
snapshot > "$work/before.json"
python3 "$entry" check | tee "$work/pre.log"
expect_status setup_required "$work/pre.log"
python3 "$entry" setup | tee "$work/setup-missing.log"
expect_status setup_required "$work/setup-missing.log"
unchanged "check/setup без окружения"

echo "== инженер создаёт окружение и ставит зависимости вне расширения"
python3 -m venv .venv
.venv/bin/python -I -m pip install --no-index --find-links "$wheelhouse" --only-binary=:all: \
  --no-deps --require-hashes -r "$ext/requirements.txt"
[ ! -e .venv/.alla-setup-complete ] || fail "появился старый маркер установки"

echo "== setup проверяет готовое окружение без записи"
snapshot > "$work/before.json"
python3 "$entry" setup | tee "$work/setup.log"
expect_status ready "$work/setup.log"
python3 "$entry" setup | tee "$work/setup2.log"
expect_status ready "$work/setup2.log"
grep -q "token set" "$work/setup.log" || fail "setup не подсказал token set"
python3 "$entry" token status | tee "$work/token-status.log"
expect_status ok "$work/token-status.log"
unchanged "setup готового окружения"

echo "== extensions enable/disable печатают инструкции без записи"
python3 "$entry" extensions enable git-publish | tee "$work/enable.log"
expect_status ok "$work/enable.log"
unchanged "extensions enable"
for args in "extensions enable git-publish --yes" "extensions disable git-publish --diff abc" \
            "setup --wheelhouse $wheelhouse" "setup --python python3"; do
  if python3 "$entry" $args > "$work/removed-option.log"; then
    fail "удалённый режим принят: $args"
  fi
  expect_status error "$work/removed-option.log"
  unchanged "удалённые флаги"
done

echo "== инженер настраивает конфигурацию расширения и .qwen/alla.env"
mkdir -p .qwen alla-extensions
printf 'enabled = ["git-publish"]\n' > alla-extensions/config.toml
snapshot > "$work/before.json"
python3 "$entry" extensions disable git-publish | tee "$work/disable.log"
expect_status ok "$work/disable.log"
unchanged "disable включённого расширения"

# .qwen/alla.env также размещает инженер; setup его не создавал.
cp "$ext/.env.example" ".qwen/alla.env"
chmod 600 ".qwen/alla.env"

echo "== check с пустым .qwen/alla.env"
code=0
python3 "$entry" check > "$work/check-empty.log" || code=$?
cat "$work/check-empty.log"
expect_status error "$work/check-empty.log"
[ "$code" = 2 ] || fail "check с пустым .qwen/alla.env вернул $code, ждали 2"

echo "== check с фейковым TestOps"
.venv/bin/python "$repo/tests/fake_testops_server.py" --port "$port" &
server_pid=$!
for _ in $(seq 50); do
  if .venv/bin/python -c "import socket; socket.create_connection(('127.0.0.1', $port), 1)" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
token=$(.venv/bin/python -c "import sys; sys.path.insert(0, sys.argv[1]); import skill_fake_testops as f; print(f.TOKEN)" \
  "$repo/tests")
printf 'ALLURE_ENDPOINT=http://127.0.0.1:%s\nALLURE_PROJECT_ID=5\n' "$port" > ".qwen/alla.env"
export ALLURE_TOKEN="$token"
printf '[testops]\nallowed_origins = ["http://127.0.0.1:%s"]\n' "$port" > alla-extensions/config.toml
python3 "$entry" check | tee "$work/check.log"
expect_status ready "$work/check.log"

echo "== prepare прогона"
for attempt in 1 2 3 4 5; do
  python3 "$entry" prepare 777 | tee "$work/prepare.log"
  # Cold imports can exceed the CLI wait budget even on a tiny synthetic launch.
  # Repeating attaches to the existing job, as in the normal agent protocol.
  if ! grep -qx 'STATUS: running' "$work/prepare.log"; then break; fi
done
expect_status analyze "$work/prepare.log"
compgen -G "alla-reports/777-*/clusters/01.md" > /dev/null || fail "prepare не записал задание кластера"

echo "OK: ручная подготовка и read-only setup расширения alla на $(uname -s) с $(python3 --version)"
