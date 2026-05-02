-- alla правила объединения кластеров — PostgreSQL DDL
-- Применить ПОСЛЕ kb_schema.sql:
--   psql -U <user> -d <dbname> -f sql/merge_rules_schema.sql

-- ---------------------------------------------------------------------------
-- Правила объединения кластеров.
--
-- Пользователь отмечает в HTML-отчёте два похожих кластера и объединяет их.
-- Правило хранит пару стабильных base_issue_signature_hash (step-agnostic).
-- Пара всегда упорядочена: signature_hash_a < signature_hash_b.
-- При следующем анализе кластеры с совпадающими сигнатурами объединяются.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS alla;

CREATE TABLE IF NOT EXISTS alla.merge_rules (
    rule_id          BIGSERIAL    PRIMARY KEY,

    -- Правила всегда привязаны к проекту.
    project_id       INTEGER      NOT NULL,

    -- Упорядоченная пара base_issue_signature.signature_hash.
    signature_hash_a TEXT         NOT NULL,
    signature_hash_b TEXT         NOT NULL,

    -- Человекочитаемый audit-текст для каждой стороны (из FeedbackClusterContext).
    audit_text_a     TEXT         NOT NULL DEFAULT '',
    audit_text_b     TEXT         NOT NULL DEFAULT '',

    -- Контекст (аудит).
    launch_id        INTEGER,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Гарантия: пара всегда хранится в лексикографическом порядке.
    CONSTRAINT chk_merge_rules_ordered CHECK (signature_hash_a < signature_hash_b)
);

-- Одно правило на упорядоченную пару в рамках проекта.
CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_rules_pair
    ON alla.merge_rules (project_id, signature_hash_a, signature_hash_b);

-- Быстрая загрузка всех правил проекта.
CREATE INDEX IF NOT EXISTS idx_merge_rules_project
    ON alla.merge_rules (project_id);

COMMENT ON TABLE  alla.merge_rules IS
    'Правила объединения кластеров: пользователь решает какие кластеры считать одной проблемой';
COMMENT ON COLUMN alla.merge_rules.project_id IS
    'ID проекта в Allure TestOps';
COMMENT ON COLUMN alla.merge_rules.signature_hash_a IS
    'base_issue_signature.signature_hash первого кластера (меньший лексикографически)';
COMMENT ON COLUMN alla.merge_rules.signature_hash_b IS
    'base_issue_signature.signature_hash второго кластера (больший лексикографически)';
COMMENT ON COLUMN alla.merge_rules.audit_text_a IS
    'Человекочитаемый audit-текст первого кластера';
COMMENT ON COLUMN alla.merge_rules.audit_text_b IS
    'Человекочитаемый audit-текст второго кластера';
