import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  use: { baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5173", trace: "retain-on-failure" },
  webServer: process.env.E2E_BASE_URL ? undefined : {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
  },
  timeout: 45000,
});
