-- alla feedback базы знаний — PostgreSQL DDL
-- Применить ПОСЛЕ kb_schema.sql:
--   psql -U <user> -d <dbname> -f sql/kb_feedback_schema.sql

-- ---------------------------------------------------------------------------
-- Таблица обратной связи: like / dislike на KB-совпадения из HTML-отчёта.
--
-- Exact memory связывает KB-запись (entry_id) с issue_signature_hash.
-- Один голос на пару (entry_id, issue_signature_hash). Повторный голос — UPSERT.
-- Старое поле error_text сохраняется только как audit-текст для отладки.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alla.kb_feedback (
    feedback_id       BIGSERIAL    PRIMARY KEY,

    -- FK на суррогатный PK alla.kb_entry.entry_id (не на slug!).
    -- Однозначно идентифицирует запись — не путает глобальные и проектные.
    kb_entry_id       BIGINT       NOT NULL
                                   REFERENCES alla.kb_entry(entry_id)
                                   ON DELETE CASCADE,

    -- Аудит-текст exact issue signature. Не участвует в matching.
    error_text        TEXT         NOT NULL,

    -- Legacy-хэш audit_text. Оставлен для совместимости/аудита.
    error_text_hash   TEXT         GENERATED ALWAYS AS (md5(error_text)) STORED,

    -- Стабильная signature текущего issue, по которой работает exact memory.
    issue_signature_hash     TEXT,
    issue_signature_version  INTEGER      NOT NULL DEFAULT 1,
    issue_signature_payload  JSONB,

    -- Тип голоса.
    -- 'like'    → exact memory pin
    -- 'dislike' → exact memory hide
    vote              TEXT         NOT NULL CHECK (vote IN ('like', 'dislike')),

    -- Контекст (аудит, не участвует в matching).
    launch_id         INTEGER,
    cluster_id        TEXT,

    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

ALTER TABLE alla.kb_feedback
    ADD COLUMN IF NOT EXISTS issue_signature_hash TEXT;

ALTER TABLE alla.kb_feedback
    ADD COLUMN IF NOT EXISTS issue_signature_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE alla.kb_feedback
    ADD COLUMN IF NOT EXISTS issue_signature_payload JSONB;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'kb_feedback_kb_entry_id_error_text_hash_key'
    ) THEN
        ALTER TABLE alla.kb_feedback
            DROP CONSTRAINT kb_feedback_kb_entry_id_error_text_hash_key;
    END IF;
END
$$;

COMMENT ON TABLE  alla.kb_feedback IS
    'Exact feedback memory тестировщиков: like/dislike на KB-совпадения из HTML-отчёта alla';
COMMENT ON COLUMN alla.kb_feedback.kb_entry_id IS
    'FK на alla.kb_entry.entry_id — суррогатный PK, не slug';
COMMENT ON COLUMN alla.kb_feedback.error_text IS
    'Audit-текст exact issue signature';
COMMENT ON COLUMN alla.kb_feedback.error_text_hash IS
    'Legacy md5(error_text) — хранится для обратной совместимости и аудита';
COMMENT ON COLUMN alla.kb_feedback.issue_signature_hash IS
    'Stable hash exact issue signature; основной ключ feedback memory';
COMMENT ON COLUMN alla.kb_feedback.issue_signature_version IS
    'Версия алгоритма exact issue signature';
COMMENT ON COLUMN alla.kb_feedback.issue_signature_payload IS
    'Опциональный JSON payload с metadata issue signature';
COMMENT ON COLUMN alla.kb_feedback.vote IS
    'like = pin exact KB match, dislike = hide exact KB match';

-- Индекс для быстрой загрузки всех голосов по entry_id.
CREATE INDEX IF NOT EXISTS idx_kb_feedback_entry_id
    ON alla.kb_feedback (kb_entry_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_feedback_issue_signature
    ON alla.kb_feedback (kb_entry_id, issue_signature_hash);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_issue_signature
    ON alla.kb_feedback (issue_signature_hash, issue_signature_version);
