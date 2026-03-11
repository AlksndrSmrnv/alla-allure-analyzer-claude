-- alla Knowledge Base Feedback — PostgreSQL DDL
-- Применить ПОСЛЕ kb_schema.sql:
--   psql -U <user> -d <dbname> -f sql/kb_feedback_schema.sql

-- ---------------------------------------------------------------------------
-- Таблица обратной связи: like / dislike на KB-совпадения из HTML-отчёта.
--
-- Связывает KB-запись (entry_id) с текстом ошибки (error_text).
-- Один голос на пару (entry_id, md5(error_text)). Повторный голос — UPSERT.
-- Fuzzy matching: при поиске голосов используется TF-IDF cosine similarity
-- между текущей ошибкой и сохранёнными error_text.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alla.kb_feedback (
    feedback_id       BIGSERIAL    PRIMARY KEY,

    -- FK на суррогатный PK alla.kb_entry.entry_id (не на slug!).
    -- Однозначно идентифицирует запись — не путает глобальные и проектные.
    kb_entry_id       BIGINT       NOT NULL
                                   REFERENCES alla.kb_entry(entry_id)
                                   ON DELETE CASCADE,

    -- Нормализованный текст ошибки (assertion message + application log, без stack trace).
    -- Используется для fuzzy matching при поиске голосов для новых ошибок.
    error_text        TEXT         NOT NULL,

    -- md5 hash error_text для эффективного UNIQUE constraint на длинных текстах.
    -- GENERATED ALWAYS: автоматически вычисляется PostgreSQL.
    error_text_hash   TEXT         GENERATED ALWAYS AS (md5(error_text)) STORED,

    -- Тип голоса.
    -- 'like'    → повысить score записи для похожих ошибок (boost пропорционален similarity)
    -- 'dislike' → понизить или исключить запись для похожих ошибок
    vote              TEXT         NOT NULL CHECK (vote IN ('like', 'dislike')),

    -- Контекст (аудит, не участвует в matching).
    launch_id         INTEGER,
    cluster_id        TEXT,

    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Один голос на (запись, нормализованный текст). UPSERT при повторном клике.
    UNIQUE (kb_entry_id, error_text_hash)
);

COMMENT ON TABLE  alla.kb_feedback IS
    'Обратная связь тестировщиков: like/dislike на KB-совпадения из HTML-отчёта alla';
COMMENT ON COLUMN alla.kb_feedback.kb_entry_id IS
    'FK на alla.kb_entry.entry_id — суррогатный PK, не slug';
COMMENT ON COLUMN alla.kb_feedback.error_text IS
    'Нормализованный текст ошибки (assertion + log). Для fuzzy TF-IDF matching';
COMMENT ON COLUMN alla.kb_feedback.error_text_hash IS
    'md5(error_text) — generated column для UNIQUE constraint';
COMMENT ON COLUMN alla.kb_feedback.vote IS
    'like = boost score, dislike = penalize/exclude from results';

-- Индекс для быстрой загрузки всех голосов по entry_id.
CREATE INDEX IF NOT EXISTS idx_kb_feedback_entry_id
    ON alla.kb_feedback (kb_entry_id);
