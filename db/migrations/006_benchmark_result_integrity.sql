-- JSON null is a non-NULL SQL jsonb value. Reject it explicitly.
ALTER TABLE benchmark_trials DROP CONSTRAINT benchmark_result_contract;
ALTER TABLE benchmark_trials ADD CONSTRAINT benchmark_result_contract CHECK (
 result IS NULL OR (
  jsonb_typeof(result)='object'
  AND COALESCE(result->>'schema_version'='2.0',false)
  AND COALESCE(result->>'verdict' IN ('PASS','FAIL','INDETERMINATE'),false)
  AND result ? 'failure_category'
  AND (
   (result->>'verdict'='PASS' AND result->>'failure_category' IS NULL)
   OR (result->>'verdict'='FAIL' AND result->>'failure_category'='agent')
   OR (result->>'verdict'='INDETERMINATE' AND result->>'failure_category' IN ('evaluator','infrastructure','cancelled'))
  ) IS TRUE
 ) IS TRUE
);
ALTER TABLE benchmark_evaluations ADD CONSTRAINT benchmark_metrics_contract CHECK (
 result IS NULL OR (jsonb_typeof(result)='object' AND COALESCE(result->>'schema_version'='2.0',false)) IS TRUE
);
