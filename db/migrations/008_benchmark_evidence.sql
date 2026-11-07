-- New evaluator v4 runs have an independent transactional evidence witness.
-- No historical rows or verdicts are modified or backfilled.
CREATE TABLE benchmark_evidence_heads (
 trial_id uuid PRIMARY KEY REFERENCES benchmark_trials(id),
 event_count integer NOT NULL DEFAULT 0 CHECK(event_count >= 0),
 digest text NOT NULL DEFAULT repeat('0',64) CHECK(length(digest)=64)
);
CREATE TABLE benchmark_receipts (
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id),
 sequence integer NOT NULL CHECK(sequence>0), payload jsonb NOT NULL,
 PRIMARY KEY(trial_id,sequence)
);
CREATE TRIGGER fence_evidence_heads BEFORE INSERT OR UPDATE ON benchmark_evidence_heads
 FOR EACH ROW EXECUTE FUNCTION fence_benchmark_event();
CREATE TRIGGER fence_evidence_receipts BEFORE INSERT OR UPDATE ON benchmark_receipts
 FOR EACH ROW EXECUTE FUNCTION fence_benchmark_event();
