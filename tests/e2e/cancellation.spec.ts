import { test, expect } from "@playwright/test";
for (const [agent, outcome, count] of [
  ["safe", "passed", "1"],
  ["duplicate", "failed", "2"],
]) {
  test(`${agent} agent: ${outcome}`, async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Reference agent").selectOption(agent);
    await page.getByRole("button", { name: "Start evaluation" }).click();
    await expect(page.getByTestId("outcome")).toHaveText(outcome, {
      timeout: 30000,
    });
    await expect(page.getByTestId("side-effects")).toHaveText(count);
    await expect(
      page.getByRole("list").getByText("timeout after commit", { exact: true }),
    ).toBeVisible();
    await page.getByText("Trace and immutable references").click();
    await expect(page.getByText("Scenario SHA-256")).toBeVisible();
    const id = await page.locator(".run-id").innerText();
    const response = await page.request.get("/api/v1/runs/" + id);
    const run = await response.json();
    expect(run.events).toHaveLength(2);
    expect(run.result.trace_id).toMatch(/^[a-f0-9]{32}$/);
    expect(
      run.events.every(
        (event: { trace_id: string }) => event.trace_id === run.result.trace_id,
      ),
    ).toBe(true);
    expect(run.events[0].side_effect_committed).toBe(true);
    expect(run.result.side_effect_count).toBe(Number(count));
    await page.reload();
    await page
      .getByRole("button")
      .filter({ hasText: id.slice(0, 8) })
      .click();
    await expect(page.getByTestId("outcome")).toHaveText(outcome);
  });
}
test("API rejects invalid input and unknown runs", async ({ request }) => {
  expect(
    (
      await request.post("/api/v1/runs", { data: { agent: "external" } })
    ).status(),
  ).toBe(400);
  expect((await request.get("/api/v1/runs/not-a-uuid")).status()).toBe(400);
  expect(
    (
      await request.get("/api/v1/runs/00000000-0000-0000-0000-000000000000")
    ).status(),
  ).toBe(404);
});
