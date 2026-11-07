-- New records only. Historical verdicts remain unchanged and unbound.
CREATE TABLE execution_source_snapshots (
 sha256 text PRIMARY KEY CHECK(length(sha256)=64),
 inventory jsonb NOT NULL,
 content jsonb NOT NULL
);
CREATE TABLE benchmark_execution_provenance (
 trial_id uuid PRIMARY KEY REFERENCES benchmark_trials(id),
 sha256 text NOT NULL CHECK(length(sha256)=64),
 manifest jsonb NOT NULL,
 source_sha256 text NOT NULL REFERENCES execution_source_snapshots(sha256),
 result_sha256 text CHECK(result_sha256 IS NULL OR length(result_sha256)=64)
);
CREATE TABLE run_execution_provenance (
 run_id uuid PRIMARY KEY REFERENCES runs(id),
 sha256 text NOT NULL CHECK(length(sha256)=64),
 manifest jsonb NOT NULL,
 source_sha256 text NOT NULL REFERENCES execution_source_snapshots(sha256),
 result_sha256 text CHECK(result_sha256 IS NULL OR length(result_sha256)=64)
);
CREATE FUNCTION guard_execution_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
  IF current_database()='agentproof_test' AND current_setting('agentproof.test_cleanup', true)='on' THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'immutable execution provenance';
 END IF;
 IF TG_OP='UPDATE' AND (OLD.manifest IS DISTINCT FROM NEW.manifest OR OLD.sha256 IS DISTINCT FROM NEW.sha256 OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256 OR OLD.result_sha256 IS NOT NULL OR NEW.result_sha256 IS NULL) THEN
  RAISE EXCEPTION 'immutable execution provenance';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_benchmark_execution_provenance BEFORE UPDATE OR DELETE ON benchmark_execution_provenance FOR EACH ROW EXECUTE FUNCTION guard_execution_provenance();
CREATE TRIGGER guard_run_execution_provenance BEFORE UPDATE OR DELETE ON run_execution_provenance FOR EACH ROW EXECUTE FUNCTION guard_execution_provenance();
CREATE FUNCTION guard_source_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable source snapshot'; END $$;
CREATE TRIGGER guard_source_snapshot BEFORE UPDATE OR DELETE ON execution_source_snapshots FOR EACH ROW EXECUTE FUNCTION guard_source_snapshot();
