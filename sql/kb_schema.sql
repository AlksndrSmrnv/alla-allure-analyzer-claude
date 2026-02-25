-- alla Knowledge Base — PostgreSQL DDL
-- Применить: psql -U <user> -d <dbname> -f sql/kb_schema.sql

CREATE SCHEMA IF NOT EXISTS alla;

COMMENT ON SCHEMA alla IS 'alla test-failure triage — knowledge base';

-- ---------------------------------------------------------------------------
-- Тип-enum для категории первопричины.
-- Зеркалирует RootCauseCategory в src/alla/knowledge/models.py.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'root_cause_category') THEN
        CREATE TYPE alla.root_cause_category AS ENUM ('test', 'service', 'env', 'data');
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Основная таблица записей базы знаний
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alla.kb_entry (
    -- Суррогатный первичный ключ.
    -- id (slug) НЕ является PK, потому что одинаковый slug может существовать
    -- как глобально (project_id IS NULL), так и для конкретного проекта
    -- (project_id = N), что позволяет проектным записям переопределять глобальные.
    entry_id         BIGSERIAL                 PRIMARY KEY,

    -- Slug-идентификатор записи, например 'connection_timeout'.
    -- Уникален внутри каждого пространства: глобального и каждого проекта.
    -- Соответствует полю KBEntry.id в Python-модели.
    id               TEXT                      NOT NULL,

    -- Заголовок проблемы для отображения в отчётах и KB-push комментариях.
    title            TEXT                      NOT NULL,

    -- Подробное описание проблемы (может быть пустой строкой, не NULL).
    description      TEXT                      NOT NULL DEFAULT '',

    -- Большой фрагмент ошибки из реального лога — основа TF-IDF-сопоставления.
    -- Соответствует KBEntry.error_example.
    error_example    TEXT                      NOT NULL,

    -- Категория первопричины: test | service | env | data.
    category         alla.root_cause_category  NOT NULL,

    -- Упорядоченный список шагов по устранению.
    -- Хранится как нативный PostgreSQL-массив TEXT[].
    -- psycopg3 автоматически приводит TEXT[] к list[str] в Python.
    resolution_steps TEXT[]                    NOT NULL DEFAULT '{}',

    -- NULL  → глобальная запись (видна всем проектам)
    -- N > 0 → запись только для проекта Allure TestOps с данным ID
    --          (аналог per-project файла project_{id}.yaml)
    --          Проектная запись с тем же slug переопределяет глобальную в Python-коде.
    project_id       INTEGER                   NULL,

    -- Аудит-поля
    created_at       TIMESTAMPTZ               NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ               NOT NULL DEFAULT now()
);

COMMENT ON TABLE  alla.kb_entry IS
    'Известные шаблоны ошибок автотестов с рекомендациями по устранению';
COMMENT ON COLUMN alla.kb_entry.entry_id         IS
    'Суррогатный PK. Slug id не является PK, т.к. один slug может существовать '
    'как глобально, так и для конкретного проекта';
COMMENT ON COLUMN alla.kb_entry.id               IS
    'Slug записи, например connection_timeout. Уникален внутри project_id.';
COMMENT ON COLUMN alla.kb_entry.error_example    IS
    'Большой фрагмент лога для TF-IDF-сопоставления с ошибками тестов';
COMMENT ON COLUMN alla.kb_entry.resolution_steps IS
    'Упорядоченные шаги по устранению проблемы (массив TEXT)';
COMMENT ON COLUMN alla.kb_entry.project_id       IS
    'NULL = глобальная запись; N = только для проекта Allure TestOps с ID N';

-- Уникальность глобальных записей: один slug может встречаться в глобальном
-- пространстве ровно один раз.
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entry_id_global
    ON alla.kb_entry (id)
    WHERE project_id IS NULL;

-- Уникальность проектных записей: один slug может встречаться для каждого
-- конкретного проекта ровно один раз.
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_entry_id_project
    ON alla.kb_entry (id, project_id)
    WHERE project_id IS NOT NULL;

-- Индекс для фильтрации по project_id (основной паттерн запроса в PostgresKnowledgeBase)
CREATE INDEX IF NOT EXISTS idx_kb_entry_project_id
    ON alla.kb_entry (project_id);
