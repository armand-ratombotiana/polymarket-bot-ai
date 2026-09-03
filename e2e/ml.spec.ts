import { test, expect } from '@playwright/test'

/**
 * ML panel E2E tests.
 *
 * Covers the three Intelligence sub-panels whose primary content is ML
 * model state — AI / ML Engine, ML Validation, and Shadow Inference.
 * Each is loaded via `next/dynamic` (`lazyPanel` in page.tsx:130-146),
 * so the test must wait for the lazy chunk to load before asserting on
 * panel content.
 *
 * The ML metrics these panels display (Brier score, ROC AUC, log loss,
 * ECE, PSI drift, etc.) come from the bot backend's `/api/ml/*`
 * endpoints. The backend may or may not be running, so tests assert
 * STRUCTURE not VALUES — e.g. "the panel rendered some heading text"
 * rather than "Brier = 0.182".
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('ML Panels', () => {
  test('can navigate to AI / ML Engine panel', async ({ page }) => {
    // Sidebar.tsx:114 — `label: 'AI / ML Engine'`. Match on the prefix
    // so the test survives minor wording tweaks (e.g. "AI/ML Engine"
    // vs "AI / ML Engine"). Use `.first()` defensively — the sidebar
    // is the only nav surface for this label.
    const aimlBtn = page.getByRole('button', { name: /AI \/ ML Engine|AI\/ML/i }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    // The lazy-loaded chunk may take a moment to download + parse.
    // The page-area wrapper persists across swaps — assert it's still
    // visible as the proxy "panel mounted" signal.
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to ML Validation panel', async ({ page }) => {
    // Sidebar.tsx:117 — `label: 'ML Validation'`.
    const validationBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await validationBtn.click()
    await expect(validationBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Shadow Inference panel', async ({ page }) => {
    // Sidebar.tsx:116 — `label: 'Shadow Inference'`.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('AI / ML Engine panel renders without crashing', async ({ page }) => {
    // AIMLCommandCenter.tsx makes 4 parallel fetches on mount
    // (/api/ml/metrics, /api/ml/drift, /api/ml/calibration, /api/ml/feature-importance).
    // When the backend is down, the Promise.all rejects, the catch
    // handler sets error state, and the panel renders its error card
    // — NOT a crash. Verify neither the root ErrorBoundary
    // (.error-boundary-fallback) nor the PanelErrorBoundary
    // (.panel-error-boundary) is visible.
    const aimlBtn = page.getByRole('button', { name: /AI \/ ML Engine|AI\/ML/i }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')

    // Give the lazy chunk + the 4 fetch promises a beat to settle.
    // 2s is enough for both the chunk-load and the rejected-promise
    // microtask queue to drain.
    await page.waitForTimeout(2000)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    // The page-area itself must still be visible (the panel mounted).
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('ML Validation panel renders without crashing', async ({ page }) => {
    const validationBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await validationBtn.click()
    await expect(validationBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('Shadow Inference panel renders without crashing', async ({ page }) => {
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('ML Validation panel surfaces metric labels when backend is up', async ({
    page,
    request,
  }) => {
    // Conditional contract: if the backend `/api/ml/metrics` endpoint is
    // unreachable, the panel renders its error / empty card without the
    // Brier / AUC labels. We can't assert their presence without the
    // backend — so probe the endpoint first and skip if down (mirrors
    // the api-health.spec.ts pattern).
    const probe = await request
      .get('/api/ml/metrics?XTransformPort=8080', { failOnStatusCode: false, timeout: 10000 })
      .catch(() => null)
    if (!probe || probe.status() !== 200) {
      test.skip(true, 'Backend /api/ml/metrics not reachable — skipping metric-label assertion')
      return
    }

    const validationBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await validationBtn.click()
    await expect(validationBtn).toHaveAttribute('aria-current', 'page')

    // The MLValidationPanel renders metric cards with labels like
    // "Brier Score" and "ROC AUC" (case-insensitive — verify by text
    // fragment). Use `getByText` with a regex that matches the typical
    // visible label, allowing for trailing "(lower is better)" hints.
    await expect(page.getByText(/brier/i).first()).toBeVisible({ timeout: 15000 })
  })
})
