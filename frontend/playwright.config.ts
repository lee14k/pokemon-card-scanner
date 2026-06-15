import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: { baseURL: "http://127.0.0.1:8900" },
  webServer: {
    command: "bash e2e/run_server.sh",
    url: "http://127.0.0.1:8900/health",
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
