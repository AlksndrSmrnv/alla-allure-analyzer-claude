#!/bin/sh
# Reads secrets mounted by secman as files under /app/secrets and exports
# each file as an environment variable. File name = variable name,
# file content = variable value.
#
# Expected layout (one secret per file):
#   /app/secrets/ALLURE_TOKEN
#   /app/secrets/ALLURE_KB_POSTGRES_DSN
#   /app/secrets/ALLURE_LANGFLOW_API_KEY
#
# The directory is optional: if secman is disabled or the path does not
# exist the script still starts the application normally.

set -e

SECRETS_DIR="${SECRETS_DIR:-/app/secrets}"

if [ -d "$SECRETS_DIR" ]; then
  for secret_file in "$SECRETS_DIR"/*; do
    [ -f "$secret_file" ] || continue

    var_name="$(basename "$secret_file")"

    # Skip files whose names are not valid shell variable identifiers
    # (e.g. hidden files, files with dots or spaces).
    case "$var_name" in
      *[!A-Za-z0-9_]*) continue ;;
    esac

    var_value="$(cat "$secret_file")"
    export "$var_name=$var_value"
  done
fi

exec "$@"
