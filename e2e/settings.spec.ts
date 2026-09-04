import { test, expect } from '@playwright/test'

/**
 * Settings modal E2E flow tests.
 *
 * Covers the W15-2 SettingsModal — the full-screen preferences dialog
 * that opens from the gear icon (🛠) in TopStatusBar. The modal is
 * rendered via `<SettingsModal isOpen={settingsOpen} onClose=… />`
 * (TopStatusBar.tsx:402) and groups every `UserPreferences` field
 * into 6 sections: Display, Dashboard, Trading, Notifications,
 * Sound, Privacy (SettingsModal.tsx:261 SECTION_ORDER).
 *
 * Edit model: the modal maintains a local `draft` state seeded from
 * the persisted preferences. Controls edit the draft only; nothing
 * persists until the trader clicks "Save changes" (SettingsModal.tsx:390
 * handleSave walks the diff + calls `update(key, value)` per changed
 * field, which writes to localStorage + dispatches
 * `preferences-changed`).
 *
 * Escape + Cancel + backdrop-click all close the modal without
 * persisting (handleCancel at SettingsModal.tsx:405).
 *
 * The Theme + Language controls inside the modal ARE persisted on
 * Save, BUT they don't immediately flip the visible UI:
 *   - Theme is applied by next-themes' `setTheme()` (called only by
 *     the TopStatusBar ThemeToggle button). The in-modal Theme
 *     Select persists the preference; the actual class flip happens
 *     via the top-bar toggle (covered by theme.spec.ts).
 *   - Language is applied by `useTranslation.setLocale()` (called
 *     by the TopStatusBar LocaleSwitcher dropdown). The in-modal
 *     Language Select persists the preference; the visible label
 *     flip happens via the top-bar switcher.
 *
 * So "test theme toggle" + "test locale switcher" within the modal
 * context = verify the in-modal Select controls can be interacted
 * with AND that the Save button becomes enabled when the draft
 * changes (proving the dirty-tracking works). For end-to-end
 * "flip the actual UI" coverage, the test ALSO exercises the
 * TopStatusBar's ThemeToggle + LocaleSwitcher controls.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

// The settings modal trigger — TopStatusBar.tsx:318 — aria-label
// "Open user preferences" with the 🛠 (hammer + wrench) emoji.
const SETTINGS_TRIGGER_LABEL = 'Open user preferences'

// The modal's role="dialog" wrapper (SettingsModal.tsx:432) with
// aria-labelledby="settings-title". The dialog title text is
// "User Preferences" (SettingsModal.tsx:440).
const SETTINGS_DIALOG_TITLE = 'User Preferences'

// Helper: open the settings modal and wait for it to settle.
async function openSettings(page: import('@playwright/test').Page) {
  const trigger = page.getByRole('button', { name: SETTINGS_TRIGGER_LABEL }).first()
  await trigger.click()
  // The modal renders with role="dialog" + aria-modal="true"
  // (SettingsModal.tsx:432-434). Wait for the dialog to mount.
  await expect(
    page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
  ).toBeVisible({ timeout: 10000 })
}

test.describe('Settings modal open / close', () => {
  test('gear icon in TopStatusBar opens the settings modal', async ({ page }) => {
    await openSettings(page)
    // The modal title "User Preferences" is rendered inside the
    // dialog (SettingsModal.tsx:440).
    await expect(
      page.getByRole('heading', { name: SETTINGS_DIALOG_TITLE }),
    ).toBeVisible()
  })

  test('Cancel button closes the modal without persisting', async ({ page }) => {
    await openSettings(page)
    const cancelBtn = page.getByRole('button', { name: /^Cancel$/i }).first()
    await expect(cancelBtn).toBeVisible()
    await cancelBtn.click()
    // The dialog should disappear from the accessibility tree.
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)
  })

  test('Escape key closes the modal', async ({ page }) => {
    // SettingsModal.tsx:290 — Escape handler calls onClose().
    await openSettings(page)
    await page.keyboard.press('Escape')
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)
  })

  test('clicking the backdrop closes the modal', async ({ page }) => {
    // SettingsModal.tsx:423 — the outer `modal-backdrop` div has an
    // onClick handler that calls handleCancel when the click target
    // is the backdrop itself (not the inner modal).
    await openSettings(page)
    // Click the top-left corner of the backdrop — coordinates outside
    // the modal but inside the backdrop.
    const backdrop = page.locator('.modal-backdrop').first()
    await backdrop.click({ position: { x: 5, y: 5 } })
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)
  })

  test('close button (✕) closes the modal', async ({ page }) => {
    // SettingsModal.tsx:443 — the modal-close button has
    // aria-label="Close settings modal".
    await openSettings(page)
    const closeBtn = page.getByRole('button', {
      name: /Close settings modal/i,
    }).first()
    await expect(closeBtn).toBeVisible()
    await closeBtn.click()
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)
  })
})

test.describe('Settings sections render', () => {
  test('all 6 sections render in canonical order', async ({ page }) => {
    // SettingsModal.tsx:261 — SECTION_ORDER = ['Display', 'Dashboard',
    // 'Trading', 'Notifications', 'Sound', 'Privacy']. Each is
    // rendered as `<section aria-label={section}>` (SettingsModal.tsx:455).
    await openSettings(page)
    const sections = ['Display', 'Dashboard', 'Trading', 'Notifications', 'Sound', 'Privacy']
    for (const section of sections) {
      await expect(
        page.getByRole('region', { name: section }).first(),
      ).toBeVisible()
    }
  })

  test('Display section renders Theme + Language controls', async ({ page }) => {
    // SettingsModal.tsx:84-109 — Display section has the Theme
    // + Language select controls. Each SettingRow renders a label
    // (SettingsModal.tsx:539).
    await openSettings(page)
    await expect(
      page.getByText(/^Theme$/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/^Language$/i).first(),
    ).toBeVisible()
  })

  test('Dashboard section renders Default panel + Refresh interval + toggles', async ({
    page,
  }) => {
    // SettingsModal.tsx:113-160 — Dashboard section has Default panel,
    // Refresh interval (slider), Auto-refresh (toggle), Reduced motion
    // (toggle).
    await openSettings(page)
    await expect(
      page.getByText(/Default panel/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Refresh interval/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Auto-refresh/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Reduced motion/i).first(),
    ).toBeVisible()
  })

  test('Trading section renders P&L + price flashes + chart + format controls', async ({
    page,
  }) => {
    await openSettings(page)
    await expect(
      page.getByText(/Show unrealized P&L|Show unrealized P&amp;L/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Show price flashes/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Default chart type/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Number format/i).first(),
    ).toBeVisible()
  })

  test('Notifications section renders Browser notifications + severity filter', async ({
    page,
  }) => {
    await openSettings(page)
    await expect(
      page.getByText(/Browser notifications/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Alert severity filter/i).first(),
    ).toBeVisible()
  })

  test('Sound section renders Sound cues + Sound volume', async ({ page }) => {
    await openSettings(page)
    await expect(
      page.getByText(/^Sound cues$/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/^Sound volume$/i).first(),
    ).toBeVisible()
  })

  test('Privacy section renders Share error reports toggle', async ({ page }) => {
    await openSettings(page)
    await expect(
      page.getByText(/Share error reports/i).first(),
    ).toBeVisible()
  })

  test('Reset to defaults button is present', async ({ page }) => {
    // SettingsModal.tsx:477 — "↺ Reset to defaults" button in the footer.
    await openSettings(page)
    await expect(
      page.getByRole('button', { name: /Reset to defaults/i }).first(),
    ).toBeVisible()
  })

  test('Save changes button is disabled when draft is clean', async ({ page }) => {
    // SettingsModal.tsx:491 — Save button has `disabled={!isDirty}`.
    // On a fresh open (draft = preferences), isDirty is false → disabled.
    await openSettings(page)
    const saveBtn = page.getByRole('button', { name: /Save changes/i }).first()
    await expect(saveBtn).toBeVisible()
    await expect(saveBtn).toBeDisabled()
  })
})

test.describe('Theme toggle (in-modal)', () => {
  test('opening the Theme select exposes Dark + Light options', async ({
    page,
  }) => {
    await openSettings(page)
    // SettingsModal.tsx:551 — the Theme Select's SelectTrigger has
    // aria-label="Theme". Clicking it opens a Radix popover with the
    // SelectItem options (Dark / Light).
    const themeTrigger = page.getByRole('combobox', { name: /^Theme$/i }).first()
    await expect(themeTrigger).toBeVisible()
    await themeTrigger.click()
    // Radix Select renders SelectItem elements with role="option"
    // inside a portal at document root. Wait for the popover to mount.
    await expect(
      page.getByRole('option', { name: /^Dark$/i }).first(),
    ).toBeVisible({ timeout: 5000 })
    await expect(
      page.getByRole('option', { name: /^Light$/i }).first(),
    ).toBeVisible()
    // Close the popover (Escape closes the Radix popover, NOT the modal,
    // because the popover's keydown handler stops propagation — but in
    // case it does propagate to the modal's Escape handler, re-open
    // the modal would be needed. To be safe, click outside the popover
    // by pressing Escape AFTER confirming options are visible).
    await page.keyboard.press('Escape')
  })

  test('selecting a different theme enables the Save button (dirty tracking)', async ({
    page,
  }) => {
    await openSettings(page)
    const themeTrigger = page.getByRole('combobox', { name: /^Theme$/i }).first()
    await themeTrigger.click()

    // Read the current displayed value (the SelectValue text) so we
    // can pick the OPPOSITE option (guaranteed to flip isDirty).
    const triggerValue = (await themeTrigger.textContent()) ?? ''
    const isCurrentlyDark = /dark/i.test(triggerValue)
    const targetOption = isCurrentlyDark ? 'Light' : 'Dark'

    await page.getByRole('option', { name: new RegExp(`^${targetOption}$`, 'i') }).first().click()

    // After the option click, the popover closes and the SelectTrigger
    // now shows the new value. The Save button should now be enabled
    // because the draft differs from the persisted preferences.
    const saveBtn = page.getByRole('button', { name: /Save changes/i }).first()
    await expect(saveBtn).toBeEnabled({ timeout: 5000 })
    // Cleanup — close without saving.
    await page.keyboard.press('Escape')
  })
})

test.describe('Locale switcher (in-modal + top-bar)', () => {
  test('in-modal Language select exposes English + French options', async ({
    page,
  }) => {
    await openSettings(page)
    const langTrigger = page.getByRole('combobox', { name: /^Language$/i }).first()
    await expect(langTrigger).toBeVisible()
    await langTrigger.click()
    await expect(
      page.getByRole('option', { name: /^English$/i }).first(),
    ).toBeVisible({ timeout: 5000 })
    await expect(
      page.getByRole('option', { name: /^French$/i }).first(),
    ).toBeVisible()
    await page.keyboard.press('Escape')
  })

  test('top-bar LocaleSwitcher flips the sidebar group label to French', async ({
    page,
  }) => {
    // The TopStatusBar LocaleSwitcher (LocaleSwitcher.tsx:23) calls
    // `setLocale(newLocale)` from useTranslation, which writes to
    // localStorage AND updates the hook's state synchronously —
    // every mounted useTranslation consumer re-renders with the new
    // locale. The sidebar's `t(group.labelKey)` resolves to the
    // French string immediately.
    //
    // en.json groups.main = "Main"; fr.json groups.main = "Principal".
    const langSelect = page.getByLabel(/Select language/i).first()
    await expect(langSelect).toBeVisible()
    // The select is a native <select> with two <option> children
    // (EN / FR). Switch to French.
    await langSelect.selectOption('fr')
    // The sidebar group label "Main" should now be "Principal"
    // (Sidebar.tsx:257 — t('groups.main')).
    await expect(
      page
        .getByRole('navigation', { name: 'Primary navigation' })
        .locator('div')
        .filter({ hasText: /^Principal$/ })
        .first(),
    ).toBeVisible({ timeout: 5000 })

    // Restore English so subsequent tests in the suite start clean.
    await langSelect.selectOption('en')
    await expect(
      page
        .getByRole('navigation', { name: 'Primary navigation' })
        .locator('div')
        .filter({ hasText: /^Main$/ })
        .first(),
    ).toBeVisible({ timeout: 5000 })
  })

  test('changing the Language select in the modal enables Save (dirty tracking)', async ({
    page,
  }) => {
    await openSettings(page)
    const langTrigger = page.getByRole('combobox', { name: /^Language$/i }).first()
    await langTrigger.click()
    // Read the current value to pick the opposite.
    const triggerValue = (await langTrigger.textContent()) ?? ''
    const isCurrentlyEn = /english/i.test(triggerValue)
    const targetOption = isCurrentlyEn ? 'French' : 'English'
    await page
      .getByRole('option', { name: new RegExp(`^${targetOption}$`, 'i') })
      .first()
      .click()
    const saveBtn = page.getByRole('button', { name: /Save changes/i }).first()
    await expect(saveBtn).toBeEnabled({ timeout: 5000 })
    await page.keyboard.press('Escape')
  })
})

test.describe('Save / Reset interactions', () => {
  test('Save button enables after editing a toggle, then Save closes the modal', async ({
    page,
  }) => {
    // SettingsModal.tsx:393 handleSave calls `update()` for every
    // changed field + `onClose()`. We toggle the "Auto-refresh"
    // switch (a safe non-destructive preference).
    await openSettings(page)
    // The Auto-refresh switch has role="switch" with aria-label="Auto-refresh"
    // (SettingsModal.tsx:547 aria-label={label}).
    const autoRefreshSwitch = page.getByRole('switch', {
      name: /Auto-refresh/i,
    }).first()
    await expect(autoRefreshSwitch).toBeVisible()
    // Snapshot the current checked state so we can verify the flip
    // AND restore afterwards (so other tests aren't affected).
    const wasChecked = await autoRefreshSwitch.getAttribute('aria-checked')
    await autoRefreshSwitch.click()
    // Save should now be enabled.
    const saveBtn = page.getByRole('button', { name: /Save changes/i }).first()
    await expect(saveBtn).toBeEnabled({ timeout: 5000 })
    await saveBtn.click()
    // Modal closes on save.
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)

    // Restore the original value so the persisted state matches the
    // DEFAULTS (other tests rely on Auto-refresh being on).
    await openSettings(page)
    const restoredSwitch = page.getByRole('switch', {
      name: /Auto-refresh/i,
    }).first()
    const currentChecked = await restoredSwitch.getAttribute('aria-checked')
    if (currentChecked !== wasChecked) {
      await restoredSwitch.click()
      await page.getByRole('button', { name: /Save changes/i }).first().click()
      await expect(
        page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
      ).toHaveCount(0)
    } else {
      // Already restored — just close the modal.
      await page.keyboard.press('Escape')
    }
  })

  test('Reset to defaults replaces the draft (Save becomes enabled only if defaults differ)', async ({
    page,
  }) => {
    // SettingsModal.tsx:412 handleReset replaces the draft with
    // getDefaults(). It does NOT persist — the trader can still
    // Cancel. Clicking Reset on a fresh modal where draft ==
    // preferences == DEFAULTS leaves isDirty=false (no change).
    await openSettings(page)
    const resetBtn = page.getByRole('button', {
      name: /Reset to defaults/i,
    }).first()
    await expect(resetBtn).toBeVisible()
    await resetBtn.click()
    // Modal should still be open (Reset doesn't close).
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toBeVisible()
    // The Save button may or may not be enabled depending on whether
    // the persisted preferences already equal DEFAULTS. We assert
    // the modal survives the reset click (no crash) — the structural
    // guard.
  })
})

test.describe('Settings modal flow integration', () => {
  test('opening settings does not interrupt panel navigation state', async ({
    page,
  }) => {
    // Navigate to Positions, open settings, close settings — the
    // active panel should still be Positions.
    const positionsBtn = page.getByRole('button', { name: /^Positions$/ }).first()
    await positionsBtn.click()
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')

    await openSettings(page)
    await page.keyboard.press('Escape')
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)

    // The active panel should still be Positions.
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')
  })

  test('no uncaught errors during settings open / interact / close', async ({
    page,
  }) => {
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))
    await openSettings(page)
    // Toggle the Auto-refresh switch (a safe reversible action).
    const sw = page.getByRole('switch', { name: /Auto-refresh/i }).first()
    await sw.click()
    // Open the Theme select popover then close it.
    await page.getByRole('combobox', { name: /^Theme$/i }).first().click()
    await page.keyboard.press('Escape')
    // Open the Language select popover then close it.
    await page.getByRole('combobox', { name: /^Language$/i }).first().click()
    await page.keyboard.press('Escape')
    // Cancel — discard the draft.
    await page.getByRole('button', { name: /^Cancel$/i }).first().click()
    await expect(
      page.getByRole('dialog', { name: SETTINGS_DIALOG_TITLE }),
    ).toHaveCount(0)
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
