CREATE TABLE benchmark_datasets (
 id text NOT NULL, version integer NOT NULL, sha256 text NOT NULL UNIQUE, content jsonb NOT NULL,
 published_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(id,version)
);
CREATE FUNCTION immutable_publication() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'published dataset is immutable'; END $$;
CREATE TRIGGER immutable_dataset BEFORE UPDATE OR DELETE ON benchmark_datasets
 FOR EACH ROW EXECUTE FUNCTION immutable_publication();
CREATE TABLE benchmark_evaluations (
 id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','cancelled')),
 manifest jsonb NOT NULL, result jsonb,
 CHECK((status IN ('completed','cancelled'))=(result IS NOT NULL))
);
CREATE TABLE benchmark_requests (
 key text PRIMARY KEY, request jsonb NOT NULL, evaluation_id uuid NOT NULL UNIQUE REFERENCES benchmark_evaluations(id)
);
CREATE TABLE benchmark_trials (
 id uuid PRIMARY KEY, evaluation_id uuid NOT NULL REFERENCES benchmark_evaluations(id),
 scenario_id text NOT NULL, trial_index integer NOT NULL CHECK(trial_index>0),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed')),
 scenario jsonb NOT NULL, state jsonb NOT NULL, started_at timestamptz, completed_at timestamptz,
 result jsonb, agent_outcome jsonb, UNIQUE(evaluation_id,scenario_id,trial_index),
 CHECK((status='completed')=(result IS NOT NULL))
);
CREATE TABLE benchmark_events (
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id), sequence integer NOT NULL CHECK(sequence>0),
 operation_key text NOT NULL UNIQUE, payload jsonb NOT NULL, PRIMARY KEY(trial_id,sequence), UNIQUE(trial_id,operation_key)
);
CREATE TABLE benchmark_effects (
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id), operation_key text NOT NULL UNIQUE,
 kind text NOT NULL, payload jsonb NOT NULL,
 FOREIGN KEY(trial_id,operation_key) REFERENCES benchmark_events(trial_id,operation_key) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE benchmark_attempts (
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id), workflow_run_id text NOT NULL,
 activity_id text NOT NULL, attempt integer NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz, outcome text, error text,
 PRIMARY KEY(trial_id,workflow_run_id,activity_id,attempt)
);
CREATE FUNCTION protect_benchmark_evaluation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.manifest IS DISTINCT FROM NEW.manifest THEN RAISE EXCEPTION 'benchmark manifest is immutable'; END IF;
 IF OLD.result IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'terminal evaluation is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_benchmark BEFORE UPDATE ON benchmark_evaluations FOR EACH ROW EXECUTE FUNCTION protect_benchmark_evaluation();
CREATE FUNCTION protect_benchmark_trial() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.scenario IS DISTINCT FROM NEW.scenario THEN RAISE EXCEPTION 'trial scenario is immutable'; END IF;
 IF OLD.result IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'terminal trial is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_benchmark_trial BEFORE UPDATE ON benchmark_trials FOR EACH ROW EXECUTE FUNCTION protect_benchmark_trial();
CREATE FUNCTION fence_benchmark_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE terminal jsonb; parent_status text;
BEGIN
 SELECT e.status INTO parent_status FROM benchmark_evaluations e JOIN benchmark_trials t ON t.evaluation_id=e.id WHERE t.id=NEW.trial_id FOR SHARE OF e;
 SELECT result INTO terminal FROM benchmark_trials WHERE id=NEW.trial_id FOR UPDATE;
 IF parent_status IN ('completed','cancelled') OR terminal IS NOT NULL THEN RAISE EXCEPTION 'benchmark trial is terminal'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER fence_benchmark_events BEFORE INSERT OR UPDATE ON benchmark_events FOR EACH ROW EXECUTE FUNCTION fence_benchmark_event();
CREATE TRIGGER fence_benchmark_effects BEFORE INSERT OR UPDATE ON benchmark_effects FOR EACH ROW EXECUTE FUNCTION fence_benchmark_event();
