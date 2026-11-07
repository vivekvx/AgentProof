-- Keep v1 identities unchanged. Version 6 uses a distinct broker key namespace.
ALTER TABLE benchmark_events DROP CONSTRAINT benchmark_operation_identity;
ALTER TABLE benchmark_events ADD CONSTRAINT benchmark_operation_identity CHECK (
 operation_key=trial_id::text || ':support-tools-v1:' || sequence
 OR operation_key=trial_id::text || ':support-tools-v2:' || sequence
);

CREATE FUNCTION guard_benchmark_operation_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE version_number integer;
BEGIN
 SELECT (e.manifest->'dataset'->>'version')::integer INTO version_number
 FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id
 WHERE t.id=NEW.trial_id;
 IF (version_number=6 AND NEW.operation_key<>(NEW.trial_id::text || ':support-tools-v2:' || NEW.sequence))
  OR (version_number<>6 AND NEW.operation_key<>(NEW.trial_id::text || ':support-tools-v1:' || NEW.sequence))
 THEN RAISE EXCEPTION 'broker operation namespace differs from published dataset'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_benchmark_operation_version BEFORE INSERT OR UPDATE ON benchmark_events
 FOR EACH ROW EXECUTE FUNCTION guard_benchmark_operation_version();
