CREATE TABLE benchmark_external_sessions (
    trial_id uuid PRIMARY KEY REFERENCES benchmark_trials(id),
    evaluation_id uuid NOT NULL REFERENCES benchmark_evaluations(id),
    scenario_id text NOT NULL,
    attempt integer NOT NULL CHECK (attempt > 0),
    state text NOT NULL CHECK (state IN ('waiting','completed','expired','cancelled')),
    credential_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    outcome jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
