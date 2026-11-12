-- A known provider quota wait is shared by all workers in the bounded study.
ALTER TABLE benchmark_study_budgets
    ADD COLUMN provider_not_before timestamptz;
