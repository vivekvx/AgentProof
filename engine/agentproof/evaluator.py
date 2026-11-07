from .contracts import Check, EvaluationResult, Manifest


def evaluate(conn, manifest: Manifest, trace_id: str) -> EvaluationResult:
    status = conn.execute(
        "SELECT status FROM orders WHERE run_id=%s", (manifest.run_id,)
    ).fetchone()[0]
    count = conn.execute(
        "SELECT count(*) FROM side_effects WHERE run_id=%s", (manifest.run_id,)
    ).fetchone()[0]
    fault = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tool_events WHERE run_id=%s AND payload->>'outcome'='timeout_after_commit' AND payload->>'side_effect_committed'='true')",
        (manifest.run_id,),
    ).fetchone()[0]
    expected = manifest.scenario.content["assertions"]
    checks = [
        Check(
            name="final_status",
            passed=status == expected["final_status"],
            expected=expected["final_status"],
            actual=status,
        ),
        Check(
            name="side_effect_count",
            passed=count == expected["side_effect_count"],
            expected=expected["side_effect_count"],
            actual=count,
        ),
        Check(name="timeout_after_commit", passed=fault, expected=True, actual=fault),
    ]
    passed = all(c.passed for c in checks)
    return EvaluationResult(
        run_id=manifest.run_id,
        status="passed" if passed else "failed",
        failure_kind=None if passed else "agent",
        checks=checks,
        final_state={
            "order_id": manifest.scenario.content["order_id"],
            "status": status,
        },
        side_effect_count=count,
        scenario_sha256=manifest.scenario.sha256,
        evaluator_sha256=manifest.evaluator.sha256,
        trace_id=trace_id,
    )
