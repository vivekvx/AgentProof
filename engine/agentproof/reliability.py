"""Internal failure evidence; v1 result enums remain compatible."""

from dataclasses import dataclass

from .database import connect


class EvaluatorFailure(RuntimeError):
    pass


class ExecutionStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class Attempt:
    workflow_id: str = "local"
    temporal_run_id: str = "local"
    activity_id: str = "execute_evaluation"
    number: int = 1

    def attributes(self, run_id, manifest):
        return {
            "agentproof.run_id": run_id,
            "agentproof.scenario.id": manifest.scenario.id,
            "agentproof.scenario.sha256": manifest.scenario.sha256,
            "agentproof.evaluator.sha256": manifest.evaluator.sha256,
            "temporal.workflow.id": self.workflow_id,
            "temporal.workflow.run_id": self.temporal_run_id,
            "temporal.activity.id": self.activity_id,
            "temporal.activity.attempt": self.number,
        }


def journal(run_id, attempt, outcome=None, error=None, dsn=None):
    with connect(dsn) as conn:
        conn.execute(
            """INSERT INTO run_attempts(run_id,temporal_run_id,activity_id,attempt,workflow_id)
          VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                run_id,
                attempt.temporal_run_id,
                attempt.activity_id,
                attempt.number,
                attempt.workflow_id,
            ),
        )
        if outcome:
            conn.execute(
                """UPDATE run_attempts SET finished_at=now(),outcome=%s,error_type=%s,message=%s
              WHERE run_id=%s AND temporal_run_id=%s AND activity_id=%s AND attempt=%s AND finished_at IS NULL""",
                (
                    outcome,
                    type(error).__name__ if error else None,
                    str(error)[:2000] if error else None,
                    run_id,
                    attempt.temporal_run_id,
                    attempt.activity_id,
                    attempt.number,
                ),
            )
