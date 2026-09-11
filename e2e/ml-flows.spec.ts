import { test, expect } from '@playwright/test'

/**
 * ML flow E2E tests (expanded coverage).
 *
 * Sibling file to `ml.spec.ts` — that file covers the basic "panel
 * becomes active + page-area survives + no crash" smoke for the 3
 * Intelligence-group ML panels. THIS file goes deeper into each
 * panel's visible structure (model info header, validation metric
 * cards, challenger models table) so a regression that leaves the
 * panel mounted but missing its content is caught.
 *
 * The three panels covered:
 *   - AI / ML Engine   (AIMLCommandCenter.tsx)
 *   - ML Validation    (MLValidationPanel.tsx)
 *   - Shadow Inference (ShadowInferencePanel.tsx)
 *
 * All three load via `lazyPanel` (page.tsx:133-134) and fetch ML
 * telemetry on mount (the AIMLCommandCenter fires 4 parallel fetches;
 * MLValidationPanel fires 3; ShadowInferencePanel fires 5). Tests
 * assert STRUCTURE — panel mounted + a stable header label is visible
 * + no PanelErrorBoundary fallback — never specific metric values
 * (Brier, AUC, etc. depend on backend state).
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

// The AI/ML Engine sidebar label is "AI / ML Engine" (Sidebar.tsx:117)
// with the slash surrounded by spaces. Match flexibly so wording
// tweaks don't break the test.
const AIML_NAV_PATTERN = /AI\s*\/\s*ML Engine/i

test.describe('AI / ML Engine flow', () => {
  test('navigates to the AI / ML Engine panel', async ({ page }) => {
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('panel renders the model telemetry header', async ({ page }) => {
    // AIMLCommandCenter.tsx:172 — "AI / ML Quantitative Telemetry &
    // Gated Model Registry" header text. Always rendered (constant
    // JSX in the header block).
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/AI \/ ML Quantitative Telemetry/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel renders model info (ensemble weights + active version)', async ({
    page,
  }) => {
    // The header shows the active model version badge
    // (AIMLCommandCenter.tsx:186 — `Active: {registry?.active_version
    // || 'v1.champion'}`) AND the Adaptive Ensemble Blend Weights card
    // (AIMLCommandCenter.tsx:249). Both are always rendered — the
    // version badge falls back to 'v1.champion' and the weights fall
    // back to the default `{ rf: 0.40, gb: 0.35, sgd: 0.05, lgbm: 0.20 }`
    // object when the fetch is in-flight or rejected.
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/Active:/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/Adaptive Ensemble Blend Weights/i).first(),
    ).toBeVisible()
  })

  test('panel renders the 38-feature pipeline badge', async ({ page }) => {
    // AIMLCommandCenter.tsx:175 — "38-Feature Pipeline" badge.
    // Always rendered (constant JSX).
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/38-Feature Pipeline/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel renders the ensemble member cards (RF / GB / SGD / LightGBM)', async ({
    page,
  }) => {
    // AIMLCommandCenter.tsx:259+ — the 4 ensemble-member cards render
    // "Random Forest", "Gradient Boost", and the SGD + LightGBM
    // member labels. All 4 are always rendered (the weight values
    // fall back to defaults).
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/^Random Forest$/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/^Gradient Boost$/i).first(),
    ).toBeVisible()
  })

  test('panel renders the Gated Retrain button', async ({ page }) => {
    // AIMLCommandCenter.tsx:200 — "⚡ Gated Retrain" button. Always
    // rendered (the disabled state flips only during retraining).
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByRole('button', { name: /Gated Retrain/i }).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel mounts without crashing', async ({ page }) => {
    // 4 parallel fetches (/api/ml/metrics, /api/ml/drift,
    // /api/ml/calibration, /api/ml/feature-importance) all settle.
    // Promise.allSettled would be more defensive than Promise.all
    // but the catch handler in fetchData covers the rejection case
    // — verify no PanelErrorBoundary fallback surfaces.
    const aimlBtn = page.getByRole('button', { name: AIML_NAV_PATTERN }).first()
    await aimlBtn.click()
    await expect(aimlBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })
})

test.describe('ML Validation flow', () => {
  test('navigates to the ML Validation panel', async ({ page }) => {
    // Sidebar.tsx:120 — `label: 'ML Validation'`.
    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('panel renders the validation header', async ({ page }) => {
    // MLValidationPanel.tsx:426 — "ML Validation & Walk-Forward CV"
    // header. Always rendered (the header is constant JSX; the
    // body's loading / error / data states vary but the header
    // text is fixed).
    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/ML Validation/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel renders the governance + drift badge', async ({ page }) => {
    // MLValidationPanel.tsx:428 — "governance + drift" badge next to
    // the header. Always rendered (constant JSX).
    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/governance \+ drift/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('validation metrics render (Brier, AUC, Log Loss, ECE, Accuracy)', async ({
    page,
    request,
  }) => {
    // Conditional: the metric cards are rendered only after the
    // /api/ml/metrics fetch resolves with a payload. When the
    // backend is down, the panel renders its ErrorState card (no
    // metric labels visible). Probe the endpoint first; if down,
    // skip the assertion (the "panel mounts without crashing" test
    // below still guards the down-path).
    const probe = await request
      .get('/api/ml/metrics?XTransformPort=8080', {
        failOnStatusCode: false,
        timeout: 10000,
      })
      .catch(() => null)
    if (!probe || probe.status() !== 200) {
      test.skip(true, 'Backend /api/ml/metrics not reachable — skipping metric-label assertion')
      return
    }

    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')

    // MLValidationPanel.tsx:502 — Brier ↓ card label.
    await expect(page.getByText(/Brier/i).first()).toBeVisible({
      timeout: 15000,
    })
    // MLValidationPanel.tsx:511 — ROC-AUC ↑ label.
    await expect(page.getByText(/ROC-AUC/i).first()).toBeVisible()
    // MLValidationPanel.tsx:520 — Log-loss ↓ label.
    await expect(page.getByText(/Log-loss/i).first()).toBeVisible()
    // MLValidationPanel.tsx:529 — ECE ↓ label.
    await expect(page.getByText(/ECE/i).first()).toBeVisible()
    // MLValidationPanel.tsx:538 — Accuracy label.
    await expect(page.getByText(/Accuracy/i).first()).toBeVisible()
  })

  test('panel exposes the Refresh control', async ({ page }) => {
    // MLValidationPanel.tsx:452 — Refresh button (with RefreshCw
    // icon). Always rendered in the header. Accessible name is
    // "Refresh" (icon + text).
    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByRole('button', { name: /^Refresh$/i }).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel mounts without crashing', async ({ page }) => {
    // 3 parallel fetches (/api/ml/metrics, /api/ml/drift,
    // /api/ml/versions) all settle. The fetchAll catch handler
    // surfaces the error via ErrorState — verify no
    // PanelErrorBoundary fallback appears.
    const valBtn = page.getByRole('button', { name: /ML Validation/i }).first()
    await valBtn.click()
    await expect(valBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })
})

test.describe('Shadow Inference flow', () => {
  test('navigates to the Shadow Inference panel', async ({ page }) => {
    // Sidebar.tsx:119 — `label: 'Shadow Inference'`.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('panel renders the shadow inference header', async ({ page }) => {
    // ShadowInferencePanel.tsx:641 — "Shadow Inference +
    // Counterfactual Journal" card-title. Rendered in the loading
    // skeleton (line 614 — "Shadow Inference") AND in the main
    // render (line 641 — full title). Match the prefix so either
    // state satisfies the assertion.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/Shadow Inference/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('challenger models section renders', async ({ page }) => {
    // ShadowInferencePanel.tsx:711 — "Challenger Models" header,
    // with a count badge `({challengers.length})`. The challengers
    // array is derived from the /api/ml/versions response; when
    // empty (or fetch rejected), it's `[]` and the count badge
    // shows "(0)". The header itself is always rendered.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/^Challenger Models$/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('champion badge renders when versions resolved', async ({
    page,
    request,
  }) => {
    // Conditional: the "Champion: vX" badge (ShadowInferencePanel.tsx:650)
    // only renders when the /api/ml/versions fetch resolves with at
    // least one `is_active=true` model. When the endpoint is down or
    // returns no champion, the badge is absent — skip the assertion
    // in that case (the "header renders" test above guards the
    // header itself).
    const probe = await request
      .get('/api/ml/versions?XTransformPort=8080', {
        failOnStatusCode: false,
        timeout: 10000,
      })
      .catch(() => null)
    if (!probe || probe.status() !== 200) {
      test.skip(true, 'Backend /api/ml/versions not reachable — skipping champion badge assertion')
      return
    }
    let hasChampion = false
    try {
      const json = await probe.json()
      const versions = Array.isArray(json?.versions) ? json.versions : []
      hasChampion = versions.some(
        (v: { is_active?: boolean; status?: string }) =>
          v?.is_active === true || v?.status === 'champion' || v?.status === 'active',
      )
    } catch {
      hasChampion = false
    }
    if (!hasChampion) {
      test.skip(true, 'No champion model reported by /api/ml/versions — skipping badge assertion')
      return
    }

    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/Champion:/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Register Challenger button is present', async ({ page }) => {
    // ShadowInferencePanel.tsx:723 — "Register Challenger" button.
    // Always rendered (it toggles the registration form visibility;
    // the button itself is constant JSX).
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByRole('button', { name: /Register Challenger/i }).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('shadow vs live comparison KPIs render (Total P&L, Win Rate, Sharpe)', async ({
    page,
  }) => {
    // ShadowInferencePanel.tsx:1122+ — comparison KPI grid renders
    // "Total P&L", "Win Rate", "Sharpe Ratio" labels. These render
    // once the panel exits its loading skeleton (line 608 — when
    // `versions` is null, the skeleton is shown instead). Probe for
    // either the loading skeleton OR the KPI labels — both valid.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/Total P&L|Total P&amp;L/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/^Win Rate$/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Sharpe Ratio/i).first(),
    ).toBeVisible()
  })

  test('panel mounts without crashing', async ({ page }) => {
    // 5 parallel fetches (/api/ml/versions, /api/ml/metrics,
    // /api/ml/shadow-trades, /api/ml/comparison, /api/ml/drift) all
    // settle. Verify no PanelErrorBoundary fallback surfaces.
    const shadowBtn = page.getByRole('button', { name: /Shadow Inference/i }).first()
    await shadowBtn.click()
    await expect(shadowBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2500)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })
})

test.describe('Cross-panel ML flow', () => {
  test('every ML panel swaps without uncaught errors', async ({ page }) => {
    // Walk the 3 Intelligence-group ML panels and capture any
    // uncaught JS exceptions. Failed fetches are caught by per-panel
    // try/catch handlers (or by Promise.all's catch in AIMLCommandCenter).
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    const panelSelectors = [
      { name: AIML_NAV_PATTERN },
      { name: /ML Validation/i },
      { name: /Shadow Inference/i },
    ]
    for (const sel of panelSelectors) {
      await page.getByRole('button', sel).first().click()
      // Brief settle so the lazy chunk + initial fetches resolve
      // before the next swap.
      await page.waitForTimeout(500)
    }
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
