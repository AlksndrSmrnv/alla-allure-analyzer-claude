#!/usr/bin/env bash
# Установка скилла с нуля, как у пользователя: синтетический git-проект автотестов,
# скилл в .qwen/skills/alla-launch, `setup` создаёт .venv проекта, `check` — сначала с пустым
# .env (ждём error), затем с адресом фейкового TestOps (ждём ready), и `prepare` прогона.
# Нужны python3 (3.11+), git и сеть до индекса пакетов. Репозиторий не меняет.
#
#   bash .github/scripts/skill_setup_smoke.sh
#   docker run --rm -v "$PWD":/repo -w /repo python:3.11 bash .github/scripts/skill_setup_smoke.sh
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
project="$work/project"
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

mkdir -p "$project/.qwen/skills"
cp -R "$repo/qwen-skill/alla-launch" "$project/.qwen/skills/alla-launch"
rm -rf "$project/.qwen/skills/alla-launch/.env" "$project/.qwen/skills/alla-launch/.venv"
find "$project" -name __pycache__ -type d -prune -exec rm -rf {} +
git -C "$project" init -q
skill="$project/.qwen/skills/alla-launch"
entry="$skill/scripts/alla_skill.py"
cd "$project"

echo "== без setup команда просит setup"
python3 "$entry" check | tee "$work/pre.log"
expect_status setup_required "$work/pre.log"

echo "== setup с нуля"
python3 "$entry" setup | tee "$work/setup.log"
expect_status ready "$work/setup.log"
[ -x .venv/bin/python ] || fail "нет .venv/bin/python"
[ -f .venv/.alla-setup-complete ] || fail "нет маркера установки"
grep -qx "/.venv" .gitignore || fail "/.venv не дописан в .gitignore"
git check-ignore -q .venv || fail "git не игнорирует .venv"
[ -f "$skill/.env" ] || fail "setup не создал .env"
mode=$(python3 -c "import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))" "$skill/.env")
[ "$mode" = 0o600 ] || fail "у .env права $mode, ждали 0o600"
[ -f .qwen/commands/alla/run.md ] || fail "setup не поставил команды /alla:*"

echo "== повторный setup берёт существующий .venv"
python3 "$entry" setup | tee "$work/setup2.log"
expect_status ready "$work/setup2.log"
[ "$(grep -cx '/.venv' .gitignore)" = 1 ] || fail "/.venv дописан в .gitignore дважды"

echo "== check с пустым .env"
code=0
python3 "$entry" check > "$work/check-empty.log" || code=$?
cat "$work/check-empty.log"
expect_status error "$work/check-empty.log"
[ "$code" = 2 ] || fail "check с пустым .env вернул $code, ждали 2"

echo "== check с фейковым TestOps"
.venv/bin/python "$repo/qwen-skill/alla-launch/tests/fake_testops_server.py" --port "$port" &
server_pid=$!
for _ in $(seq 50); do
  if .venv/bin/python -c "import socket; socket.create_connection(('127.0.0.1', $port), 1)" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
token=$(.venv/bin/python -c "import sys; sys.path.insert(0, sys.argv[1]); import skill_fake_testops as f; print(f.TOKEN)" \
  "$repo/qwen-skill/alla-launch/tests")
printf 'ALLURE_ENDPOINT=http://127.0.0.1:%s\nALLURE_TOKEN=%s\n' "$port" "$token" > "$skill/.env"
python3 "$entry" check | tee "$work/check.log"
expect_status ready "$work/check.log"

echo "== prepare прогона"
python3 "$entry" prepare 777 | tee "$work/prepare.log"
expect_status analyze "$work/prepare.log"
compgen -G "alla-reports/777-*/clusters/01.md" > /dev/null || fail "prepare не записал задание кластера"

echo "OK: установка скилла на $(uname -s) с $(python3 --version)"
