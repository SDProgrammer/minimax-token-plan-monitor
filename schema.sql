-- minimax_token_plan schema
-- 在 PostgreSQL 实例上创建数据库后执行此文件:
--   psql -U postgres -d minimax_token_plan < schema.sql

CREATE TABLE IF NOT EXISTS token_plan_snapshots (
    id               BIGSERIAL PRIMARY KEY,
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_url       TEXT NOT NULL,
    raw_response     JSONB NOT NULL,
    base_status_code INT,
    base_status_msg  TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at
    ON token_plan_snapshots (captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_base_status
    ON token_plan_snapshots (base_status_code);

CREATE TABLE IF NOT EXISTS token_plan_model_usage (
    id                     BIGSERIAL PRIMARY KEY,
    snapshot_id            BIGINT NOT NULL
        REFERENCES token_plan_snapshots(id) ON DELETE CASCADE,
    model_name             TEXT NOT NULL,
    interval_start_ms      BIGINT,
    interval_end_ms        BIGINT,
    interval_remains_ms    BIGINT,
    interval_total_count   BIGINT,
    interval_usage_count   BIGINT,
    interval_remaining_pct INT,
    interval_status        INT,
    weekly_start_ms        BIGINT,
    weekly_end_ms          BIGINT,
    weekly_remains_ms      BIGINT,
    weekly_total_count     BIGINT,
    weekly_usage_count     BIGINT,
    weekly_remaining_pct   INT,
    weekly_status           INT
);
CREATE INDEX IF NOT EXISTS idx_model_usage_snapshot
    ON token_plan_model_usage (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_model_name
    ON token_plan_model_usage (model_name);
CREATE INDEX IF NOT EXISTS idx_model_usage_weekly_start
    ON token_plan_model_usage (weekly_start_ms);
