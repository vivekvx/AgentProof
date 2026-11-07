CREATE TABLE IF NOT EXISTS runs (
 id uuid PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(),
 status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','infrastructure_failed')),
 manifest jsonb NOT NULL,
 result jsonb
);
CREATE TABLE IF NOT EXISTS orders (
 run_id uuid PRIMARY KEY REFERENCES runs(id),
 order_id text NOT NULL,
 status text NOT NULL CHECK (status IN ('active','cancelled'))
);
CREATE TABLE IF NOT EXISTS side_effects (
 run_id uuid NOT NULL REFERENCES runs(id),
 ordinal integer NOT NULL,
 kind text NOT NULL,
 PRIMARY KEY(run_id,ordinal)
);
CREATE TABLE IF NOT EXISTS tool_events (
 run_id uuid NOT NULL REFERENCES runs(id),
 sequence integer NOT NULL,
 payload jsonb NOT NULL,
 PRIMARY KEY(run_id,sequence)
);
CREATE OR REPLACE FUNCTION protect_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.manifest IS DISTINCT FROM NEW.manifest THEN RAISE EXCEPTION 'manifest is immutable'; END IF;
 IF OLD.result IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'terminal run is immutable'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_run ON runs;
CREATE TRIGGER immutable_run BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION protect_run();
