-- Bind each new benchmark aggregate without changing published result contracts.
CREATE TABLE benchmark_evaluation_provenance (
 evaluation_id uuid PRIMARY KEY REFERENCES benchmark_evaluations(id),
 sha256 text NOT NULL CHECK(length(sha256)=64),
 manifest jsonb NOT NULL,
 source_sha256 text NOT NULL REFERENCES execution_source_snapshots(sha256),
 result_sha256 text CHECK(result_sha256 IS NULL OR length(result_sha256)=64)
);
CREATE TRIGGER guard_benchmark_evaluation_provenance BEFORE UPDATE OR DELETE ON benchmark_evaluation_provenance FOR EACH ROW EXECUTE FUNCTION guard_execution_provenance();
