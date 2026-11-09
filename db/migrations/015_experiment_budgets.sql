-- Durable whole-study reservations. Unknown calls retain their reservation.
CREATE TABLE benchmark_study_budgets (
    study_id text PRIMARY KEY CHECK (length(study_id) BETWEEN 1 AND 128),
    limits jsonb NOT NULL CHECK (jsonb_typeof(limits)='object'),
    reserved_slots integer NOT NULL DEFAULT 0 CHECK (reserved_slots BETWEEN 0 AND 8),
    reserved_requests integer NOT NULL DEFAULT 0 CHECK (reserved_requests BETWEEN 0 AND 48),
    reserved_tool_calls integer NOT NULL DEFAULT 0 CHECK (reserved_tool_calls BETWEEN 0 AND 80),
    reserved_tokens bigint NOT NULL DEFAULT 0 CHECK (reserved_tokens BETWEEN 0 AND 512000),
    reserved_active_ms integer NOT NULL DEFAULT 0 CHECK (reserved_active_ms BETWEEN 0 AND 40000),
    reserved_cost_usd numeric(12,6) NOT NULL DEFAULT 0 CHECK (reserved_cost_usd BETWEEN 0 AND 0.8),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE benchmark_study_reservations (
    study_id text NOT NULL REFERENCES benchmark_study_budgets(study_id),
    slot_id text NOT NULL CHECK (length(slot_id) BETWEEN 1 AND 128),
    state text NOT NULL CHECK (state IN ('reserved','settled','unknown')),
    reserved jsonb NOT NULL CHECK (jsonb_typeof(reserved)='object'),
    observed jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    PRIMARY KEY (study_id, slot_id),
    CHECK ((state='reserved' AND observed IS NULL AND settled_at IS NULL)
        OR (state IN ('settled','unknown') AND observed IS NOT NULL AND settled_at IS NOT NULL))
);
