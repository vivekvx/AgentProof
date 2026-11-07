-- Platform rejection evidence is separate from immutable execution/verdict journals.
-- It can record a denied late call without changing a terminal trial or its digest.
CREATE TABLE benchmark_broker_rejections (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 trial_id uuid NOT NULL REFERENCES benchmark_trials(id),
 sequence integer NOT NULL CHECK(sequence>=0),
 attempt integer NOT NULL CHECK(attempt>=1),
 reason text NOT NULL CHECK(reason IN ('terminal','replay_mismatch','evidence_integrity')),
 tool text NOT NULL CHECK(length(tool)<=64),
 arguments_sha256 text NOT NULL CHECK(length(arguments_sha256)=64),
 created_at timestamptz NOT NULL DEFAULT now()
);
