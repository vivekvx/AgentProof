-- Cancellation intent and Temporal request delivery are distinct from terminal child results.
ALTER TABLE benchmark_studies
 ADD COLUMN deadline_reached_at timestamptz,
 ADD COLUMN cancellation_requested_at timestamptz;

CREATE TABLE benchmark_study_cancellation_delivery (
 study_id uuid NOT NULL REFERENCES benchmark_studies(id),
 evaluation_id uuid NOT NULL REFERENCES benchmark_evaluations(id),
 requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 temporal_accepted_at timestamptz,
 error_class text,
 PRIMARY KEY(study_id,evaluation_id)
);
