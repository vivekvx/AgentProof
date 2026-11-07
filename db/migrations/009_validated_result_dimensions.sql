ALTER TABLE benchmark_trials ADD CONSTRAINT validated_result_dimensions CHECK (
 result IS NULL OR result->>'evaluator_version' IS DISTINCT FROM 'support-evaluator-v4' OR (
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
