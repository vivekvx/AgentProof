-- Admission is serialized by the API's global transaction advisory lock.
-- Completed studies retain immutable identity while a later study may reuse
-- the frozen local plan. The idempotency key remains unique per study.
DROP INDEX benchmark_one_study_per_plan;
