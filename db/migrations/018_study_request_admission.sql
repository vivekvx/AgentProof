-- Bind an immutable study slot to a trial and reserve each model request before dispatch.
CREATE TABLE benchmark_study_trials (
    trial_id uuid PRIMARY KEY REFERENCES benchmark_trials(id),
    study_id text NOT NULL REFERENCES benchmark_study_budgets(study_id),
    slot_id text NOT NULL CHECK (length(slot_id) BETWEEN 1 AND 128),
    bound_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (study_id, slot_id),
    FOREIGN KEY (study_id, slot_id) REFERENCES benchmark_study_reservations(study_id, slot_id)
);

CREATE TABLE benchmark_study_requests (
    trial_id uuid NOT NULL REFERENCES benchmark_study_trials(trial_id),
    sequence integer NOT NULL CHECK (sequence > 0),
    state text NOT NULL CHECK (state IN ('reserved', 'known', 'unknown', 'rejected')),
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens BETWEEN 0 AND 64000),
    reserved_cost_usd numeric(12,6) NOT NULL CHECK (reserved_cost_usd BETWEEN 0 AND 0.1),
    observed_tokens bigint CHECK (observed_tokens BETWEEN 0 AND 64000),
    estimated_cost_usd numeric(12,6) CHECK (estimated_cost_usd BETWEEN 0 AND 0.1),
    reported_cost_usd numeric(12,6) CHECK (reported_cost_usd >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    PRIMARY KEY (trial_id, sequence),
    CHECK ((state = 'reserved' AND settled_at IS NULL)
       OR (state <> 'reserved' AND settled_at IS NOT NULL))
);
