package server

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// localEnvelopeLock serializes study and ordinary evaluation admission across
// API processes. It is deliberately a hard rejection, not a second scheduler.
func localEnvelopeLock(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended('agentproof:local-envelope',11))")
	return err
}

func genericEvaluationActive(ctx context.Context, tx pgx.Tx) (bool, error) {
	var active bool
	err := tx.QueryRow(ctx, `SELECT EXISTS (
		SELECT 1 FROM benchmark_evaluations e
		WHERE COALESCE(e.manifest->>'study_id','') = ''
		AND (e.result IS NULL OR EXISTS (
			SELECT 1 FROM benchmark_trials t WHERE t.evaluation_id=e.id AND t.result IS NULL
		))
	)`).Scan(&active)
	return active, err
}

func studyEvaluationActive(ctx context.Context, tx pgx.Tx) (bool, error) {
	var active bool
	err := tx.QueryRow(ctx, `SELECT EXISTS (
		SELECT 1 FROM benchmark_studies s
		WHERE (SELECT count(*) FROM benchmark_study_evaluations e WHERE e.study_id=s.id) <> 2
		OR EXISTS (SELECT 1 FROM benchmark_study_evaluations e
			JOIN benchmark_evaluations b ON b.id=e.evaluation_id
			WHERE e.study_id=s.id AND b.result IS NULL)
		OR (SELECT count(*) FROM benchmark_study_trials t WHERE t.study_id=s.id::text) <> 8
		OR EXISTS (SELECT 1 FROM benchmark_study_trials st
			JOIN benchmark_trials t ON t.id=st.trial_id
			WHERE st.study_id=s.id::text AND t.result IS NULL)
	)`).Scan(&active)
	return active, err
}
