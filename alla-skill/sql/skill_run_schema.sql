-- alla.skill_run — состояние pipeline-а скилл-режима.
--
-- Скрипты alla-skill/scripts/* обмениваются данными через эту таблицу:
--   fetch_clusters    INSERT (status='clustered'),
--   submit_analysis   UPDATE (status='analyzed'),
--   generate_report   UPDATE (status='reported'),
--   push_to_testops   UPDATE (status='pushed'),
--   любой шаг при exception → record_error (status='failed').
--
-- DSN — тот же ALLURE_KB_POSTGRES_DSN, что и для KB / feedback / report.

CREATE SCHEMA IF NOT EXISTS alla;

CREATE TABLE IF NOT EXISTS alla.skill_run (
    run_id              SERIAL       PRIMARY KEY,
    schema_version      INTEGER      NOT NULL DEFAULT 1,
    status              TEXT         NOT NULL DEFAULT 'pending',
        -- pending | clustered | analyzed | reported | pushed | failed

    launch_id           INTEGER      NOT NULL,
    project_id          INTEGER      NULL,
    launch_name         TEXT         NULL,

    -- Pipeline data (filled by fetch_clusters)
    triage_json         JSONB        NOT NULL,
    clustering_json     JSONB        NULL,
    kb_results_json     JSONB        NULL,
    kb_provenance_json  JSONB        NULL,
    feedback_ctx_json   JSONB        NULL,
    onboarding_json     JSONB        NULL,

    -- Agent analysis (filled by submit_analysis)
    agent_analysis_json JSONB        NULL,
    agent_summary_text  TEXT         NULL,
    agent_submitted_at  TIMESTAMPTZ  NULL,

    -- Report (filled by generate_report)
    report_filename     TEXT         NULL,
    report_url          TEXT         NULL,
    report_generated_at TIMESTAMPTZ  NULL,

    -- Push (filled by push_to_testops)
    push_result_json    JSONB        NULL,
    pushed_at           TIMESTAMPTZ  NULL,

    -- Error tracking (any step writes here on exception)
    error_json          JSONB        NULL,

    -- Timestamps
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skill_run_launch
    ON alla.skill_run (launch_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_skill_run_status
    ON alla.skill_run (status);

CREATE INDEX IF NOT EXISTS idx_skill_run_project
    ON alla.skill_run (project_id);

-- updated_at триггер
CREATE OR REPLACE FUNCTION alla.skill_run_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS skill_run_updated_at ON alla.skill_run;
CREATE TRIGGER skill_run_updated_at
    BEFORE UPDATE ON alla.skill_run
    FOR EACH ROW EXECUTE FUNCTION alla.skill_run_touch_updated_at();
