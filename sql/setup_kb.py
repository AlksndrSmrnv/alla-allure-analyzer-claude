#!/usr/bin/env python3
"""Создание схемы и начальное наполнение таблицы alla.kb_entry.

Использует psycopg (уже входит в alla[postgres]) — psql не нужен.

Использование:
    python sql/setup_kb.py                              # DSN из .env или ALLURE_KB_POSTGRES_DSN
    python sql/setup_kb.py --env-file /path/to/.env    # указать другой .env файл
    python sql/setup_kb.py --seed-only                 # только данные (схема уже есть)
    python sql/setup_kb.py --schema-only               # только схема
    python sql/setup_kb.py --dry-run                   # показать SQL без выполнения

Если пароль содержит спецсимволы (!, $, @ и др.) — не передавайте DSN аргументом,
это вызовет ошибки bash. Вместо этого укажите DSN в файле .env:

    echo 'ALLURE_KB_POSTGRES_DSN=postgresql://user:p@$$w0rd!@host:5432/db' >> .env
    python sql/setup_kb.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# DDL — схема
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS alla;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type
                   WHERE typname = 'root_cause_category'
                     AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'alla')) THEN
        CREATE TYPE alla.root_cause_category AS ENUM ('test', 'service', 'env', 'data');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS alla.kb_entry (
    entry_id         BIGSERIAL                 PRIMARY KEY,
    id               TEXT                      NOT NULL,
    title            TEXT                      NOT NULL,
    description      TEXT                      NOT NULL DEFAULT '',
    error_example    TEXT                      NOT NULL,
    category         alla.root_cause_category  NOT NULL,
    resolution_steps TEXT[]                    NOT NULL DEFAULT '{}',
    project_id       INTEGER                   NULL,
    created_at       TIMESTAMPTZ               NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ               NOT NULL DEFAULT now()
);

COMMENT ON TABLE  alla.kb_entry IS
    'Известные шаблоны ошибок автотестов с рекомендациями по устранению';
COMMENT ON COLUMN alla.kb_entry.entry_id IS
    'Суррогатный PK. Slug id не является PK: один slug может существовать '
    'как глобально, так и для конкретного проекта';
COMMENT ON COLUMN alla.kb_entry.id IS
    'Slug записи, например connection_timeout. Уникален внутри project_id.';
COMMENT ON COLUMN alla.kb_entry.error_example IS
    'Большой фрагмент лога для TF-IDF-сопоставления с ошибками тестов';
COMMENT ON COLUMN alla.kb_entry.resolution_steps IS
    'Упорядоченные шаги по устранению проблемы (массив TEXT)';
COMMENT ON COLUMN alla.kb_entry.project_id IS
    'NULL = глобальная запись; N = только для проекта Allure TestOps с ID N';

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entry_id_global
    ON alla.kb_entry (id)
    WHERE project_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entry_id_project
    ON alla.kb_entry (id, project_id)
    WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kb_entry_project_id
    ON alla.kb_entry (project_id);
"""

# ---------------------------------------------------------------------------
# DML — начальные данные
# Каждый элемент: (id, title, description, error_example, category,
#                  resolution_steps, project_id)
# ---------------------------------------------------------------------------

SEED_ENTRIES = [
    # --- из knowledge_base/entries.yaml ---
    (
        "connection_timeout",
        "Таймаут подключения к сервису",
        "Тесты не могут установить TCP-соединение с целевым сервисом. "
        "Сервис не запущен или недоступен по сети.",
        """\
java.net.SocketTimeoutException: Connect timed out
    at java.net.Socket.connect(Socket.java:601)
    at org.apache.http.conn.socket.PlainConnectionSocketFactory.connectSocket(PlainConnectionSocketFactory.java:75)
Caused by: Connection timed out connecting to service-endpoint:8080""",
        "env",
        [
            "Проверить доступность сервиса: curl -v <service-url>",
            "Проверить, что сервис запущен и прошёл health-check",
        ],
        None,
    ),
    (
        "null_pointer_exception",
        "NullPointerException в коде приложения",
        "Приложение выбрасывает NullPointerException — дефект в коде сервиса, "
        "объект не инициализирован.",
        """\
java.lang.NullPointerException: Cannot invoke method on null object reference
    at com.company.service.UserService.getUser(UserService.java:45)
    at com.company.controller.UserController.handleRequest(UserController.java:112)""",
        "service",
        [
            "Определить, какой объект оказался null (см. стек-трейс)",
            "Создать баг-репорт с точным стеком и шагами воспроизведения",
        ],
        None,
    ),
    # --- дополнительные глобальные записи ---
    (
        "assertion_error",
        "Ошибка утверждения в тесте",
        "Тест упал из-за несоответствия ожидаемого и фактического значения — "
        "дефект в тестовом коде или изменение поведения приложения.",
        """\
java.lang.AssertionError: expected:<200> but was:<404>
    at org.junit.Assert.fail(Assert.java:89)
    at org.junit.Assert.assertEquals(Assert.java:135)
AssertionError: assert response.status_code == 200
  where response.status_code = 404""",
        "test",
        [
            "Проверить, изменилось ли поведение API (статус-код, тело ответа)",
            "Сравнить ожидаемое и фактическое значение в логе теста",
            "Обновить тест, если изменение поведения намеренное",
        ],
        None,
    ),
    (
        "db_connection_error",
        "Ошибка подключения к базе данных",
        "Тесты не могут подключиться к базе данных. "
        "База данных недоступна или некорректно настроена строка подключения.",
        """\
org.postgresql.util.PSQLException: Connection refused. Check that the hostname and port are correct
    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl(ConnectionFactoryImpl.java:262)
OperationalError: could not connect to server: Connection refused
    Is the server running on host "db-host" and accepting TCP/IP connections on port 5432?""",
        "env",
        [
            "Проверить статус базы данных: pg_isready -h <host> -p <port>",
            "Проверить строку подключения в конфигурации тестового окружения",
            "Проверить сетевую доступность хоста БД из среды выполнения тестов",
        ],
        None,
    ),
    (
        "test_data_missing",
        "Отсутствуют тестовые данные",
        "Тест не нашёл необходимые данные в тестовой среде — "
        "не выполнена подготовка данных или данные были удалены.",
        """\
NoSuchElementException: No element with id=12345 found
    at com.company.repository.UserRepository.findById(UserRepository.java:78)
EntityNotFoundException: Unable to find entity with id: 99999
AssertionError: Expected test user "qa_user_001" to exist but got None""",
        "data",
        [
            "Проверить, что seed-данные были применены перед запуском тестов",
            "Запустить скрипт подготовки тестовых данных",
            "Убедиться, что тестовые данные не были удалены другими тестами",
        ],
        None,
    ),
    (
        "ssl_certificate_error",
        "Ошибка SSL-сертификата",
        "Тест не может установить HTTPS-соединение из-за проблем с SSL-сертификатом "
        "(самоподписанный, истёкший или недоверенный CA).",
        """\
javax.net.ssl.SSLHandshakeException: PKIX path building failed: unable to find valid certification path
    at sun.security.ssl.Alerts.getSSLException(Alerts.java:192)
requests.exceptions.SSLError: HTTPSConnectionPool: Max retries exceeded
Caused by: SSLCertVerificationError: certificate verify failed: self signed certificate""",
        "env",
        [
            "Проверить срок действия SSL-сертификата сервиса",
            "Для корпоративных сетей: добавить корневой CA в доверенные хранилища",
            "Временно: использовать ALLURE_SSL_VERIFY=false (только для диагностики)",
        ],
        None,
    ),
    (
        "out_of_memory_error",
        "Нехватка памяти (OutOfMemoryError)",
        "JVM или процесс тестов исчерпал доступную память. "
        "Возможные причины: утечка памяти, недостаточный heap, тяжёлые тестовые данные.",
        """\
java.lang.OutOfMemoryError: Java heap space
    at java.util.Arrays.copyOf(Arrays.java:3210)
    at com.company.service.DataProcessor.processAll(DataProcessor.java:145)
MemoryError: unable to allocate array
fatal error: runtime: out of memory""",
        "env",
        [
            "Увеличить heap JVM: -Xmx2g (или выше) в параметрах запуска тестов",
            "Проверить наличие утечек памяти в тестовом коде (не освобождаются ресурсы)",
            "Уменьшить параллелизм тестов, если память ограничена",
        ],
        None,
    ),
]

INSERT_SQL = """
INSERT INTO alla.kb_entry
    (id, title, description, error_example, category, resolution_steps, project_id)
VALUES
    (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) WHERE project_id IS NULL DO NOTHING
"""

INSERT_PROJECT_SQL = """
INSERT INTO alla.kb_entry
    (id, title, description, error_example, category, resolution_steps, project_id)
VALUES
    (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id, project_id) WHERE project_id IS NOT NULL DO NOTHING
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print(msg: str, *, prefix: str = "   ") -> None:
    print(prefix + msg, flush=True)


def _load_dotenv(env_file: str = ".env") -> None:
    """Загрузить переменные из .env файла в os.environ (если ещё не заданы).

    Поддерживает форматы:
        KEY=value
        KEY="value with spaces"
        KEY='value with $pecial ch@rs!'
        # комментарии

    Уже установленные переменные окружения не перезаписываются.
    """
    path = Path(env_file)
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            # Снять обрамляющие кавычки (одинарные или двойные)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Не перезаписывать переменные, уже заданные в окружении
            if key not in os.environ:
                os.environ[key] = value


def _resolve_dsn(dsn_arg: str | None) -> str:
    dsn = dsn_arg or os.environ.get("ALLURE_KB_POSTGRES_DSN", "")
    if not dsn:
        print(
            "Ошибка: DSN не указан.\n"
            "  Рекомендуемый способ (избегает проблем со спецсимволами в пароле):\n"
            "    Добавьте в файл .env строку:\n"
            "      ALLURE_KB_POSTGRES_DSN=postgresql://user:pass@host:5432/db\n"
            "    Затем запустите: python sql/setup_kb.py\n"
            "\n"
            "  Или укажите другой .env файл:\n"
            "    python sql/setup_kb.py --env-file /path/to/.env",
            file=sys.stderr,
        )
        sys.exit(1)
    return dsn


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    dsn: str,
    *,
    run_schema: bool = True,
    run_seed: bool = True,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print("=== DRY RUN — запросы не выполняются ===\n")
        if run_schema:
            print("--- SCHEMA SQL ---")
            print(SCHEMA_SQL.strip())
            print()
        if run_seed:
            print("--- SEED SQL ---")
            print(f"INSERT {len(SEED_ENTRIES)} глобальных записей (ON CONFLICT DO NOTHING)")
        return

    try:
        import psycopg
    except ImportError:
        print(
            "Ошибка: psycopg не установлен.\n"
            "  Установите: pip install 'alla[postgres]'\n"
            "  или:        pip install 'psycopg[binary]>=3.1'",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Подключение к БД...")
    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:
        print(f"Ошибка подключения: {exc}", file=sys.stderr)
        sys.exit(1)

    with conn:
        if run_schema:
            print("Создание схемы alla и таблицы kb_entry...")
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            print("✓ Схема создана (или уже существовала)\n")

        if run_seed:
            print(f"Загрузка {len(SEED_ENTRIES)} записей...")
            inserted = 0
            skipped = 0
            with conn.cursor() as cur:
                for entry in SEED_ENTRIES:
                    sql = INSERT_PROJECT_SQL if entry[-1] is not None else INSERT_SQL
                    cur.execute(sql, entry)
                    if cur.rowcount == 1:
                        inserted += 1
                        _print(f"+ {entry[0]}")
                    else:
                        skipped += 1
                        _print(f"~ {entry[0]} (уже существует, пропущена)")
            print(f"\n✓ Готово: добавлено {inserted}, пропущено {skipped}")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Создать схему и заполнить таблицу alla.kb_entry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "dsn",
        nargs="?",
        help="DSN подключения: postgresql://user:pass@host:5432/db. "
             "Если пароль содержит спецсимволы — используйте .env файл "
             "вместо этого аргумента.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Путь к .env файлу с ALLURE_KB_POSTGRES_DSN (по умолчанию: .env)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--schema-only",
        action="store_true",
        help="Только создать схему, не загружать данные",
    )
    group.add_argument(
        "--seed-only",
        action="store_true",
        help="Только загрузить данные (схема уже создана)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать SQL без выполнения",
    )
    args = parser.parse_args()

    # Загрузить .env до resolve_dsn, чтобы ALLURE_KB_POSTGRES_DSN была доступна
    _load_dotenv(args.env_file)

    # DSN не нужен для --dry-run: подключения к БД не происходит
    dsn = "" if args.dry_run else _resolve_dsn(args.dsn)
    run_schema = not args.seed_only
    run_seed = not args.schema_only

    run(dsn, run_schema=run_schema, run_seed=run_seed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
