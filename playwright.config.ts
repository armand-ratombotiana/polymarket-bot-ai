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
 * - `timeout: 60_000` (per-test) + `expect.timeout: 15_000` — generous
 *   enough for the lazy-loaded panel chunks (each takes 1–2s to
 *   download + parse on first hit) plus the `waitForTimeout(2000)`
 *   settles several W23-8 flow tests use to let in-flight fetch
 *   promises reject. The default 30s test / 5s expect were too tight
 *   for cold dev-server compiles.
 * - `actionTimeout: 30_000` + `navigationTimeout: 45_000` — explicit
 *   per-action + per-navigation caps so a stuck action doesn't eat the
 *   whole per-test budget.
 * - The webServer hook waits up to 120s for the first successful GET
 *   on `http://localhost:3000`; the Next.js dev server takes ~8s to
 *   compile the initial request, so the generous timeout is
 *   intentional (doubled from the previous 60s to absorb sandbox load
 *   spikes when other test suites are running concurrently).
 */
export default defineConfig({
  testDir: './e2e',
  // Match only the .spec.ts files in testDir so any scratch or helper
  // files in the same folder aren't picked up by the runner.
  testMatch: '**/*.spec.ts',
  fullyParallel: false, // Sequential — the app has shared state
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1, // Single worker — shared backend state
  // Per-test timeout — generous because several tests do
  // waitForTimeout(2000) settles + the lazy-loaded panel chunks take
  // 1–2s to download on the first hit. The default 30s is tight when
  // the dev server is cold-compiling a previously-unseen panel.
  timeout: 60_000,
  // Per-expect timeout — for assertions that poll (e.g. badge visible
  // OR error-state visible). The default 5s is too tight for lazy
  // panels + fetch rejection microtasks; 15s gives the dev server
  // compile + chunk download + render cycle enough runway.
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // Per-action timeout — Playwright's auto-wait default is 30s; the
    // dev server's first request can take ~8s to compile, so 30s is
    // still appropriate. We set it explicitly for documentation.
    actionTimeout: 30_000,
    navigationTimeout: 45_000,
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
    // 120s — generous because the dev server's first compile can take
    // 8–12s on a cold sandbox, and the webServer hook re-polls until
    // it gets a 2xx. The previous 60s was tight when the host is under
    // load from other test suites.
    timeout: 120_000,
  },
})
