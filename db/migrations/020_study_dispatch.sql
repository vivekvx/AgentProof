-- Study identity and admission are committed with every declared trial slot.
CREATE TABLE benchmark_studies (
    id uuid PRIMARY KEY,
    plan_id text NOT NULL CHECK (length(plan_id) BETWEEN 1 AND 128),
    plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[a-f0-9]{64}$'),
    plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','cancelled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (plan->>'id' = plan_id)
);
CREATE UNIQUE INDEX benchmark_one_study_per_plan ON benchmark_studies(plan_id);

CREATE TABLE benchmark_study_evaluations (
    study_id uuid NOT NULL REFERENCES benchmark_studies(id),
    evaluation_id uuid NOT NULL UNIQUE REFERENCES benchmark_evaluations(id),
    profile_id text NOT NULL,
    PRIMARY KEY (study_id, profile_id)
);

CREATE TABLE benchmark_study_create_keys (
    key text PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 128),
    request jsonb NOT NULL,
    study_id uuid NOT NULL UNIQUE REFERENCES benchmark_studies(id)
);

CREATE FUNCTION protect_study_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.plan_id IS DISTINCT FROM NEW.plan_id
     OR OLD.plan_sha256 IS DISTINCT FROM NEW.plan_sha256
     OR OLD.plan IS DISTINCT FROM NEW.plan THEN
    RAISE EXCEPTION 'study plan is immutable';
  END IF;
  IF OLD.status = 'cancelled' AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'cancelled study is terminal';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_study_plan BEFORE UPDATE ON benchmark_studies
 FOR EACH ROW EXECUTE FUNCTION protect_study_identity();
