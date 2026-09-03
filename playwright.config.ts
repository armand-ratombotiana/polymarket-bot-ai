import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E config for the Polymarket Pro dashboard.
 *
 * Design notes:
 * - `fullyParallel: false` + `workers: 1` — the workstation shares a single
 *   backend (FastAPI on :8080 via the gateway) and the dashboard itself has
 *   global client-side state (the `useBot` hook polls `/api/snapshot` from a
 *   module-level singleton). Running tests sequentially avoids cross-test
 *   state contamination and keeps the dev server log legible.
 * - `reuseExistingServer: !process.env.CI` — in local dev the sandbox's
 *   auto-running `bun run dev` is reused; in CI a fresh server is booted.
 * - `trace: 'on-first-retry'` — full trace (DOM snapshot + network + console)
 *   kept only for retried tests to keep artifact size manageable.
 * - The webServer hook waits up to 60s for the first successful GET on
 *   `http://localhost:3000`; the Next.js dev server takes ~8s to compile the
 *   initial request, so the generous timeout is intentional.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false, // Sequential — the app has shared state
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1, // Single worker — shared backend state
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'bun run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 60000,
  },
})
