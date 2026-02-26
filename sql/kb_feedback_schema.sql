-- alla Knowledge Base Feedback — PostgreSQL DDL
-- Применить ПОСЛЕ kb_schema.sql:
--   psql -U <user> -d <dbname> -f sql/kb_feedback_schema.sql

-- ---------------------------------------------------------------------------
-- Таблица обратной связи: like / dislike на KB-совпадения из HTML-отчёта.
--
-- Связывает KB-запись (entry_id) с паттерном ошибки (error_fingerprint).
-- Один голос на пару (entry_id, fingerprint). Повторный голос — UPSERT.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alla.kb_feedback (
    feedback_id       BIGSERIAL    PRIMARY KEY,

    -- FK на суррогатный PK alla.kb_entry.entry_id (не на slug!).
    -- Однозначно идентифицирует запись — не путает глобальные и проектные.
    kb_entry_id       BIGINT       NOT NULL
                                   REFERENCES alla.kb_entry(entry_id)
                                   ON DELETE CASCADE,

    -- SHA-256 hex нормализованного текста ошибки (message + trace + logs)
    -- с версией нормализации: sha256(f"v{VERSION}:{normalize_text(error)}").
    -- Привязывает голос к конкретному паттерну ошибки, а не к запуску/тесту.
    error_fingerprint CHAR(64)     NOT NULL,

    -- Тип голоса.
    -- 'like'    → повысить score записи для этого паттерна (boost)
    -- 'dislike' → не показывать запись для этого паттерна (exclusion)
    vote              TEXT         NOT NULL CHECK (vote IN ('like', 'dislike')),

    -- Контекст (аудит, не участвует в matching).
    launch_id         INTEGER,
    cluster_id        TEXT,

    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Один голос на (запись, паттерн). UPSERT при повторном клике.
    UNIQUE (kb_entry_id, error_fingerprint)
);

COMMENT ON TABLE  alla.kb_feedback IS
    'Обратная связь тестировщиков: like/dislike на KB-совпадения из HTML-отчёта alla';
COMMENT ON COLUMN alla.kb_feedback.kb_entry_id IS
    'FK на alla.kb_entry.entry_id — суррогатный PK, не slug';
COMMENT ON COLUMN alla.kb_feedback.error_fingerprint IS
    'SHA-256 hex нормализованного error_text (message+trace+logs) с версией';
COMMENT ON COLUMN alla.kb_feedback.vote IS
    'like = boost score, dislike = exclude from results';

-- Индекс для быстрого lookup exclusions/boosts по fingerprint.
CREATE INDEX IF NOT EXISTS idx_kb_feedback_fingerprint
    ON alla.kb_feedback (error_fingerprint);
