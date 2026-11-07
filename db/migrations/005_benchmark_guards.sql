ALTER TABLE benchmark_events ADD CONSTRAINT benchmark_operation_identity
 CHECK(operation_key=trial_id::text || ':support-tools-v1:' || sequence);
ALTER TABLE benchmark_effects ADD CONSTRAINT benchmark_effect_kind
 CHECK(kind IN ('cancellation','refund','address_change','approval'));
ALTER TABLE benchmark_trials ADD CONSTRAINT benchmark_result_contract
 CHECK(result IS NULL OR (result->>'schema_version'='2.0' AND result->>'verdict' IN ('PASS','FAIL','INDETERMINATE')));
