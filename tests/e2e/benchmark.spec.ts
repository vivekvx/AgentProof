import { test, expect } from "@playwright/test";

test("published interaction dataset shows trusted reply and result", async ({ page }) => {
  test.setTimeout(90000);
  await page.goto("/");
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await page.getByLabel("Dataset version").selectOption("customer-support@6");
  await expect(page.getByLabel("Agent configuration")).toHaveValue("support-interaction-safe");
  await page.locator(".scenario-selection summary").click();
  await expect(page.getByText(/t07-001/)).toBeVisible();
  await page.getByRole("button", { name: "Run benchmark", exact: true }).click();
  await expect(page.getByTestId("benchmark-status")).toHaveText("completed", { timeout: 60000 });
  await expect(page.getByTestId("benchmark-progress")).toHaveText("5 / 5 trials");
  await page.getByRole("button", { name: "Inspect t07-001 trial 1" }).click();
  await expect(page.getByTestId("assertion-evidence")).toContainText("PASS · No failure");
  await expect(page.getByTestId("assertion-evidence")).toContainText("trusted_user_turn");
  await expect(page.getByTestId("assertion-evidence")).toContainText("support-evaluator-v5");
});

test("run repeated benchmark trials, inspect evidence, and compare agent versions", async ({
  page,
}) => {
  test.setTimeout(720000);
  await page.goto("/");
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await expect(page.getByLabel("Dataset version")).toContainText("v5");
  await page.getByLabel("Scenario split").selectOption("all");
  await page.getByLabel("Trials per scenario").fill("2");
  await page
    .getByRole("button", { name: "Run benchmark", exact: true })
    .click();
  await expect(page.getByTestId("benchmark-status")).toHaveText("completed", {
    timeout: 300000,
  });
  await expect(page.getByTestId("benchmark-progress")).toHaveText(
    "64 / 64 trials",
  );
  const safeID = await page.getByTestId("benchmark-id").innerText();
  const safe = await (
    await page.request.get("/api/v2/evaluations/" + safeID)
  ).json();
  expect(safe.metrics.task_success_rate.sample_count).toBe(64);
  expect(safe.metrics.task_success_rate.value).toBe(1);
  expect(safe.metrics.cohort.planned_slots).toBe(64);
  expect(safe.metrics.cohort.terminal_trials).toBe(64);
  expect(safe.metrics.end_to_end_valid_completion.sample_count).toBe(64);
  expect(safe.metrics.repeated_run_reliability.complete_scenarios).toBe(32);
  expect(safe.metrics.inference_cost_usd.value).toBeNull();
  expect(safe.metrics.token_usage.input).toBeNull();
  await expect(page.getByTestId("cohort-counts")).toContainText("Planned 64");
  await expect(page.getByTestId("conditional-task-success")).toHaveText("100.0%");
  await expect(page.getByTestId("end-to-end-completion")).toHaveText("100.0%");
  await expect(page.getByTestId("token-usage")).toContainText("Input Unavailable");
  await expect(page.getByTestId("estimated_cost_usd")).toHaveText("Unavailable");
  expect(new Set(safe.trials.map((t: { id: string }) => t.id)).size).toBe(64);
  await page
    .getByRole("button", { name: "Inspect cs-016 trial 1", exact: true })
    .click();
  await expect(page.getByTestId("assertion-evidence")).toContainText("PASS");
  await expect(page.getByTestId("assertion-evidence")).toContainText(
    "timeout_after_commit",
  );
  await page.getByLabel("Agent configuration").selectOption("support-blind");
  const created = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v2/evaluations") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Run benchmark", exact: true })
    .click();
  const createdID = (await (await created).json()).id;
  await expect(page.getByTestId("benchmark-id")).toHaveText(createdID);
  await expect(page.getByTestId("benchmark-status")).toHaveText("completed", {
    timeout: 300000,
  });
  const blindID = await page.getByTestId("benchmark-id").innerText();
  expect(blindID).not.toBe(safeID);
  const blind = await (
    await page.request.get("/api/v2/evaluations/" + blindID)
  ).json();
  expect(
    blind.trials.find(
      (t: { scenario_id: string; trial_index: number }) =>
        t.scenario_id === "cs-016" && t.trial_index === 1,
    ).result.effect_counts.cancellation,
  ).toBe(2);
  await page
    .getByRole("button", { name: "Inspect cs-016 trial 1", exact: true })
    .click();
  await expect(page.getByTestId("assertion-evidence")).toContainText(
    "FAIL · agent",
  );
  await expect(page.getByTestId("assertion-evidence")).toContainText(
    "side effects",
  );
  await expect(
    page.getByLabel("Baseline evaluation").locator(`option[value="${safeID}"]`),
  ).toHaveCount(1);
  await page.getByLabel("Baseline evaluation").selectOption(safeID);
  await page.getByLabel("Candidate evaluation").selectOption(blindID);
  await page
    .getByRole("button", { name: "Compare evaluations", exact: true })
    .click();
  await expect(page.getByTestId("comparison-result")).toBeVisible();
  await expect(page.getByTestId("comparison-kind")).toContainText("Offline reference-agent");
  expect(
    Number(await page.getByTestId("comparison-regression").innerText()),
  ).toBeGreaterThan(0);
  await expect(page.getByTestId("comparison-improvement")).toHaveText("0");
  await page.getByLabel("Baseline evaluation").selectOption(blindID);
  await page.getByLabel("Candidate evaluation").selectOption(safeID);
  await page
    .getByRole("button", { name: "Compare evaluations", exact: true })
    .click();
  await expect(page.getByTestId("comparison-regression")).toHaveText("0");
  expect(
    Number(await page.getByTestId("comparison-improvement").innerText()),
  ).toBeGreaterThan(0);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await page
    .getByRole("button")
    .filter({ hasText: safeID.slice(0, 8) })
    .click();
  await expect(page.getByTestId("benchmark-progress")).toHaveText(
    "64 / 64 trials",
  );
});

test("metric and comparison labels show missing evidence", async ({ page }) => {
  const baselineID = "11111111-1111-4111-8111-111111111111";
  const candidateID = "22222222-2222-4222-8222-222222222222";
  const agent = (id: string) => ({ id, version: 1, content: { title: id, prompt_version: "support-prompt-v1", tool_schema_version: "support-tools-v1" } });
  const manifest = (id: string) => ({
    agent: agent(id),
    dataset: { id: "customer-support", version: 5, sha256: "dataset" },
    split: "development",
    trials: 2,
    scenario_ids: ["a", "b", "c"],
  });
  const rate = (value: number | null, numerator: number, sample_count: number) => ({ value, numerator, sample_count, scenario_count: 2, interval: null });
  const metrics = {
    total_trials: 6, determinate_trials: 2, indeterminate_trials: 3,
    cohort: { planned_slots: 6, started_trials: 4, terminal_trials: 5, pass_trials: 1, fail_trials: 1, indeterminate_trials: 3, missing_or_pending_slots: 1, failure_classes: { agent: 1, provider: 1, evaluator: 0, infrastructure: 1, cancelled: 1 } },
    task_success_rate: rate(0.5, 1, 2),
    end_to_end_valid_completion: rate(1 / 6, 1, 6),
    business_fulfillment: rate(0.25, 1, 4),
    unsafe_action_rate: { ...rate(0.5, 1, 2), unknown_count: 4 },
    unsafe_evidence: { proven_unsafe_trials: 1, unknown_safety_slots: 4, unauthorized_attempts: 1, invalid_argument_attempts: 1, forbidden_effects: 0 },
    recovery_success_rate: rate(null, 0, 0),
    duplicate_side_effect_rate: rate(0, 0, 2),
    tool_call_validity: rate(0.5, 2, 4),
    repeated_run_reliability: { value: null, sample_count: 0, planned_scenarios: 3, excluded_scenarios: 3, planned_repeats: 6, determinate_repeats: 2, indeterminate_repeats: 3, missing_repeats: 1 },
    execution_latency_ms: { p50: 10, p95: 20, sample_count: 5, population_count: 5, missing_count: 0 },
    active_task_latency_ms: { p50: null, p95: null, sample_count: 0, population_count: 5, missing_count: 5 },
    prestart_wait_ms: { p50: 4, p95: 8, sample_count: 4, population_count: 4, missing_count: 0 },
    evaluation_elapsed_ms: { value: 100, sample_count: 1 },
    model_latency_ms: { p50: 70, p95: 96, sample_count: 5, population_count: 5, missing_count: 0 },
    token_usage: { input: 33, output: 7, sample_count: 2, request_count: 5, input_known_requests: 3, output_known_requests: 2 },
    estimated_cost_usd: { value: 0.03, sample_count: 2, request_count: 5, complete: false },
    reported_cost_usd: { value: null, sample_count: 0, request_count: 5, complete: false },
    estimated_cost_per_success_usd: { value: null, sample_count: 2, request_count: 5, success_count: 1 },
    reported_cost_per_success_usd: { value: null, sample_count: 0, request_count: 5, success_count: 1 },
    inference_cost_usd: { value: 0.03, sample_count: 2 },
    cost_per_success_usd: { value: null, sample_count: 2, success_count: 1 },
    notes: ["Descriptive only. Missing usage is unavailable."],
  };
  let responseMetrics = metrics;
  const summaries = [baselineID, candidateID].map((id) => ({ id, status: "completed", created_at: "2026-11-06T00:00:00Z", manifest: manifest(id), metrics: null, trials: [] }));
  await page.route("**/api/v2/evaluations", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: summaries });
    else await route.continue();
  });
  await page.route("**/api/v2/evaluations/*", async (route) => {
    const id = route.request().url().split("/").at(-1)!;
    await route.fulfill({ json: { id, status: "completed", created_at: "2026-11-06T00:00:00Z", manifest: manifest(id), metrics: responseMetrics, trials: [
      { id: "trial-a", scenario_id: "a", trial_index: 1, status: "completed", result: { verdict: "PASS", failure_category: null } },
      { id: "trial-a2", scenario_id: "a", trial_index: 2, status: "completed", result: { verdict: "INDETERMINATE", failure_category: "provider" } },
      { id: "trial-b", scenario_id: "b", trial_index: 1, status: "completed", result: { verdict: "FAIL", failure_category: "agent" } },
      { id: "trial-b2", scenario_id: "b", trial_index: 2, status: "completed", result: { verdict: "INDETERMINATE", failure_category: "infrastructure" } },
      { id: "trial-c", scenario_id: "c", trial_index: 1, status: "completed", result: { verdict: "INDETERMINATE", failure_category: "cancelled" } },
    ] } });
  });
  await page.route("**/api/v2/comparisons", async (route) => {
    await route.fulfill({ json: { comparison_kind: "real_model", baseline_model: "openai/gpt-oss-20b", candidate_model: "openai/gpt-oss-120b", provisional: false, comparable_count: 1, sample_count: 3, counts: { regression: 1, improvement: 0, unchanged: 0, missing: 1, indeterminate: 1 }, paired_success_delta: -1, pairs: [], notes: ["Descriptive real-model comparison; no superiority claim."] } });
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await page.getByRole("button").filter({ hasText: baselineID.slice(0, 8) }).click();
  await expect(page.getByTestId("cohort-counts")).toContainText("Pending 1");
  await expect(page.getByTestId("cohort-counts")).toContainText("Determinate 2");
  await expect(page.getByTestId("failure-classes")).toContainText("agent 1 · provider 1 · evaluator 0 · infrastructure 1 · cancelled 1");
  await expect(page.getByTestId("conditional-task-success")).toHaveText("50.0%");
  await expect(page.getByTestId("end-to-end-completion")).toHaveText("16.7%");
  await expect(page.getByTestId("business-fulfillment")).toHaveText("25.0%");
  await expect(page.getByTestId("token-usage")).toContainText("Input 33 · Output 7");
  await expect(page.getByTestId("estimated_cost_usd")).toHaveText("$0.030000");
  await expect(page.getByText("Incomplete cost coverage")).toHaveCount(2);
  await page.getByLabel("Baseline evaluation").selectOption(baselineID);
  await page.getByLabel("Candidate evaluation").selectOption(candidateID);
  await page.getByRole("button", { name: "Compare evaluations" }).click();
  await expect(page.getByTestId("comparison-kind")).toContainText("Real-model agent outcomes");
  await expect(page.getByTestId("comparison-missing")).toHaveText("1");
  await expect(page.getByTestId("comparison-indeterminate")).toHaveText("1");
  responseMetrics = { ...metrics, estimated_cost_usd: undefined, reported_cost_usd: undefined, estimated_cost_per_success_usd: undefined, reported_cost_per_success_usd: undefined };
  await page.reload();
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await page.getByRole("button").filter({ hasText: baselineID.slice(0, 8) }).click();
  await expect(page.getByText("Request coverage unavailable")).toHaveCount(2);
  await expect(page.getByText("0 / 0 requests")).toHaveCount(0);
});

test("benchmark API rejects invalid configuration and incompatible selections", async ({
  request,
}) => {
  const valid = {
    dataset_id: "customer-support",
    dataset_version: 2,
    agent_id: "support-safe",
    agent_version: 1,
    split: "development",
    trials: 1,
    scenario_ids: ["cs-001"],
  };
  for (const input of [
    { ...valid, trials: 0 },
    { ...valid, trials: null },
    { ...valid, scenario_ids: ["cs-025"] },
    { ...valid, agent_id: "external-agent" },
    { ...valid, private_assertions: {} },
  ]) {
    expect(
      (await request.post("/api/v2/evaluations", { data: input })).status(),
    ).toBe(400);
  }
  const left = await (
    await request.post("/api/v2/evaluations", { data: valid })
  ).json();
  const right = await (
    await request.post("/api/v2/evaluations", {
      data: { ...valid, scenario_ids: ["cs-002"] },
    })
  ).json();
  expect(
    (
      await request.post("/api/v2/comparisons", {
        data: { baseline_id: left.id, candidate_id: right.id },
      })
    ).status(),
  ).toBe(409);
});

test("external agent configuration issues a scoped session envelope", async ({ page }) => {
  const external = {
    id: "my-http-agent",
    version: 1,
    content: {
      title: "My independent HTTP agent",
      policy: "external",
      prompt_version: "developer-agent-v1",
      tool_schema_version: "support-tools-v1",
      model_configuration: { provider: "offline", model: "external-agent", external_calls: false },
      external_session: { transport: "http-json", session_ttl_seconds: 120 },
    },
  };
  const dataset = [{
    id: "customer-support", version: 5, title: "Customer support v5", sha256: "dataset",
    scenarios: [{ id: "cs-001", title: "Cancel an order", split: "development", category: "cancellation" }],
    review: "fixture",
  }];
  const evaluation = {
    id: "external-evaluation", status: "running", created_at: "2026-11-13T00:00:00Z",
    manifest: { agent: external, dataset: { id: "customer-support", version: 5, sha256: "dataset" }, split: "development", trials: 1, scenario_ids: ["cs-001"] },
    metrics: null,
    trials: [{ id: "external-trial", scenario_id: "cs-001", trial_index: 1, status: "running", result: null }],
  };
  await page.route("**/api/v2/datasets", async (route) => route.fulfill({ json: dataset }));
  await page.route("**/api/v2/agents", async (route) => route.fulfill({ json: [external] }));
  await page.route("**/api/v2/evaluations", async (route) => {
    if (route.request().method() === "POST") await route.fulfill({ status: 202, json: { id: evaluation.id } });
    else await route.fulfill({ json: [evaluation] });
  });
  await page.route("**/api/v2/evaluations/external-evaluation", async (route) => route.fulfill({ json: evaluation }));
  await page.route("**/api/v2/external/sessions/external-trial", async (route) => route.fulfill({
    status: 201,
    json: {
      credential: "trial-only-credential",
      task: { trial_id: "external-trial", run_id: evaluation.id, scenario_id: "cs-001", deadline_unix_ms: 1, request: "Cancel order", tools: [] },
    },
  }));
  await page.goto("/");
  await page.getByRole("tab", { name: "Benchmarks", exact: true }).click();
  await page.getByLabel("Agent configuration").selectOption(external.id);
  await expect(page.getByText("Independent agent selected")).toBeVisible();
  await page.getByRole("button", { name: "Run benchmark", exact: true }).click();
  await page.getByRole("button", { name: "Issue session", exact: true }).click();
  await expect(page.getByTestId("external-session-envelope")).toContainText("trial-only-credential");
  await expect(page.getByText("AgentProof does not accept the agent's claimed verdict.")).toBeVisible();
});
