-- A reserved call is never automatically reissued after an uncertain interruption.
CREATE TABLE benchmark_model_calls (
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id), sequence integer NOT NULL CHECK(sequence>0),
 status text NOT NULL CHECK(status IN ('pending','completed','failed')),
 response jsonb, metadata jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY(trial_id,sequence)
);
CREATE TRIGGER fence_model_calls BEFORE INSERT OR UPDATE ON benchmark_model_calls
 FOR EACH ROW EXECUTE FUNCTION fence_benchmark_event();
ALTER TABLE benchmark_trials DROP CONSTRAINT benchmark_result_contract;
ALTER TABLE benchmark_trials ADD CONSTRAINT benchmark_result_contract CHECK (
 result IS NULL OR (
  jsonb_typeof(result)='object' AND COALESCE(result->>'schema_version'='2.0',false)
  AND COALESCE(result->>'verdict' IN ('PASS','FAIL','INDETERMINATE'),false)
  AND result ? 'failure_category' AND (
   (result->>'verdict'='PASS' AND result->>'failure_category' IS NULL)
   OR (result->>'verdict'='FAIL' AND result->>'failure_category'='agent')
   OR (result->>'verdict'='INDETERMINATE' AND result->>'failure_category' IN ('provider','evaluator','infrastructure','cancelled'))
  ) IS TRUE
 ) IS TRUE
);
