CREATE UNIQUE INDEX tool_run_operation_identity ON tool_events(run_id,operation_key);
ALTER TABLE tool_events ADD CHECK(operation_key=run_id::text || ':reference-v1:tool:' || sequence);
ALTER TABLE side_effects DROP CONSTRAINT effect_has_event;
ALTER TABLE side_effects ADD CONSTRAINT effect_has_event FOREIGN KEY(run_id,operation_key)
 REFERENCES tool_events(run_id,operation_key) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE run_requests (
 idempotency_key text PRIMARY KEY CHECK(length(idempotency_key) BETWEEN 1 AND 128),
 agent text NOT NULL,
 run_id uuid NOT NULL UNIQUE REFERENCES runs(id)
);
