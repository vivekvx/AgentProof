-- Version 3 results are valid only for evaluator v5.
ALTER TABLE benchmark_trials DROP CONSTRAINT benchmark_result_contract;
ALTER TABLE benchmark_trials ADD CONSTRAINT benchmark_result_contract CHECK (
 result IS NULL OR (
  jsonb_typeof(result)='object'
  AND COALESCE(
   result->>'schema_version'='2.0'
   OR result->>'schema_version'='3.0' AND result->>'evaluator_version'='support-evaluator-v5',
   false
  )
  AND COALESCE(result->>'verdict' IN ('PASS','FAIL','INDETERMINATE'),false)
  AND result ? 'failure_category' AND (
   (result->>'verdict'='PASS' AND result->>'failure_category' IS NULL)
   OR (result->>'verdict'='FAIL' AND result->>'failure_category'='agent')
   OR (result->>'verdict'='INDETERMINATE' AND result->>'failure_category' IN ('provider','evaluator','infrastructure','cancelled'))
  ) IS TRUE
 ) IS TRUE
);
ALTER TABLE benchmark_trials ADD CONSTRAINT interaction_result_dimensions CHECK (
 result IS NULL OR result->>'evaluator_version' IS DISTINCT FROM 'support-evaluator-v5' OR (
  jsonb_typeof(result->'assessment')='object' AND jsonb_typeof(result->'provenance')='object'
  AND result->'assessment'->>'overall'=result->>'verdict'
  AND (result->'assessment'->>'policy' <> 'FAIL' OR result->>'verdict'='FAIL')
  AND (result->>'verdict'<>'PASS' OR (
   result->'assessment'->>'harness'='complete' AND result->'assessment'->>'execution'='completed'
   AND result->'assessment'->>'task'='PASS' AND result->'assessment'->>'policy'='PASS'
   AND result->'provenance'->>'references_valid'='true'
  ))
 ) IS TRUE
);
