-- Explicit operation identities. The wire-format v1 tool events remain unchanged.
ALTER TABLE tool_events ADD COLUMN operation_key text;
UPDATE tool_events SET operation_key = run_id::text || ':reference-v1:tool:' || sequence;
ALTER TABLE tool_events ALTER COLUMN operation_key SET NOT NULL;
CREATE UNIQUE INDEX tool_operation_identity ON tool_events(operation_key);
ALTER TABLE side_effects ADD COLUMN operation_key text;
WITH events AS (
 SELECT run_id,operation_key,row_number() OVER (PARTITION BY run_id ORDER BY sequence) ordinal
 FROM tool_events WHERE (payload->>'side_effect_committed')::boolean
)
UPDATE side_effects s SET operation_key=e.operation_key FROM events e
 WHERE s.run_id=e.run_id AND s.ordinal=e.ordinal;
ALTER TABLE side_effects ALTER COLUMN operation_key SET NOT NULL;
CREATE UNIQUE INDEX effect_operation_identity ON side_effects(operation_key);
ALTER TABLE side_effects ADD CONSTRAINT effect_has_event FOREIGN KEY(operation_key)
 REFERENCES tool_events(operation_key) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runs ADD CONSTRAINT consistent_terminal_state CHECK (
 (status IN ('completed','infrastructure_failed')) = (result IS NOT NULL)
);
CREATE TABLE run_failures (
 run_id uuid PRIMARY KEY REFERENCES runs(id),
 operation_key text NOT NULL UNIQUE,
 kind text NOT NULL CHECK(kind IN ('evaluator','infrastructure','cancelled')),
 error_type text NOT NULL,
 message text NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE run_attempts (
 run_id uuid NOT NULL REFERENCES runs(id),
 temporal_run_id text NOT NULL,
 activity_id text NOT NULL,
 attempt integer NOT NULL CHECK(attempt>0),
 workflow_id text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz,
 outcome text,
 error_type text,
 message text,
 PRIMARY KEY(run_id,temporal_run_id,activity_id,attempt)
);
-- Row locks fence late activity threads after a terminal result is recorded.
CREATE FUNCTION guard_simulator_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE terminal jsonb;
BEGIN
 SELECT result INTO terminal FROM runs WHERE id=NEW.run_id FOR UPDATE;
 IF terminal IS NOT NULL THEN RAISE EXCEPTION 'run is terminal'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER fence_orders BEFORE INSERT OR UPDATE ON orders
 FOR EACH ROW EXECUTE FUNCTION guard_simulator_write();
CREATE TRIGGER fence_effects BEFORE INSERT OR UPDATE ON side_effects
 FOR EACH ROW EXECUTE FUNCTION guard_simulator_write();
CREATE TRIGGER fence_events BEFORE INSERT OR UPDATE ON tool_events
 FOR EACH ROW EXECUTE FUNCTION guard_simulator_write();
