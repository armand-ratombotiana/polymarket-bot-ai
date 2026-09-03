# W13-6 — Browser Push Notifications

**Agent:** full-stack-developer
**Task ID:** W13-6
**Date:** 2025

## Task
Add browser push notifications for critical alerts, surfaced via a bell-icon toggle in the TopStatusBar. Hook polls `/api/alerts?limit=10&unacknowledged_only=true` every 30 s and fires a desktop toast when a new critical/error alert arrives.

## Work Log

1. **Read context** — worklog tail (W12-2 maintenance scripts), `src/hooks/useBot.ts` (REST-poll + WS-fallback pattern), `src/components/TopStatusBar.tsx` (right-side action button cluster), `src/lib/api.ts` (`apiFetch` + `getApiUrl` + `authHeaders` patterns), `src/hooks/useFeatureFlags.ts` (polling-with-visibility-aware pattern).

2. **Created `src/lib/notifications.ts`** — pure, framework-agnostic primitives:
   - `isNotificationSupported()` — `typeof window !== 'undefined' && 'Notification' in window`.
   - `getPermission()` — returns `'denied'` when not supported, otherwise `Notification.permission`.
   - `requestPermission()` — async, short-circuits when already `'granted'`, otherwise awaits `Notification.requestPermission()`.
   - `showNotification(title, options?)` — guards on support + permission; constructs a `Notification` with `icon: '/icon.svg'`, `badge: '/icon.svg'`; sets up a 10 s `setTimeout(close)` auto-dismiss; wires `onclick` to `window.focus()` + `close()`.
   - `showCriticalAlert(alert)` — severity-emoji prefix map (🚨/❌/⚠️/ℹ️/🔔 fallback); `tag: alert-${name}`; `requireInteraction: severity === 'critical'`.
   - `showTradeNotification(trade)` — 📈/📉 based on `side`; body format `SIDE size @ price.toFixed(4) — token_id.slice(0,12)...`.

3. **Created `src/hooks/useNotifications.ts`** — React hook orchestrating the lifecycle:
   - State: `permission`, `enabled`, `lastAlertIds: Set<string>`.
   - On mount: reads `Notification.permission` and persisted `localStorage['notifications_enabled']`; only re-enables polling if `permission === 'granted'` AND stored flag is `'true'` (handles browser-side permission revocation between sessions).
   - Polling effect (deps `[enabled, lastAlertIds]`): when enabled AND tab visible, calls `apiFetch('/api/alerts?limit=10&unacknowledged_only=true')`; filters for `severity === 'critical' || 'error'`; calls `showCriticalAlert()` per new alert; updates `lastAlertIds` (capped at 50 entries); `setInterval(poll, 30_000)`.
   - `enable()` — awaits `requestPermission()`; on granted flips state, persists flag, fires a "Notifications Enabled" test toast.
   - `disable()` — flips state, persists flag as `'false'` (does NOT revoke browser permission — impossible from JS).
   - `toggle()` — delegates to enable/disable based on current state.

4. **Modified `src/components/TopStatusBar.tsx`** — added notification bell button to the right-side action cluster:
   - Imported `useNotifications` hook.
   - Conditional rendering: button only renders when `notifications.supported === true` (so unsupported browsers don't see a dead button).
   - Three-state visual:
     - `permission === 'denied'`: gray, `disabled`, tooltip "Permission denied — re-enable via browser site settings".
     - `enabled === true`: green 🔔, tooltip "Browser notifications ON — click to disable".
     - `enabled === false` (default/granted): gray 🔕, tooltip "Enable browser push notifications".
   - `aria-pressed={notifications.enabled}` for screen-reader state announcement; `aria-label` flips per state.
   - Placed between the audio mute button (🔊/🔇) and the keyboard-shortcuts button (⌨️) so all "alert/notification" controls are grouped together.

5. **Created `src/lib/notifications.test.ts`** — 24 tests covering every primitive:
   - `isNotificationSupported`: true when `Notification` on window, false otherwise.
   - `getPermission`: 'denied' when not supported, otherwise returns `Notification.permission`.
   - `requestPermission`: short-circuits when already granted; awaits `Notification.requestPermission()` otherwise; forwards denied.
   - `showNotification`: no-ops when not supported/permission not granted; constructs with default icon/badge; 10 s auto-close via `vi.useFakeTimers()` + `advanceTimersByTime(10_000)`; `onclick` focuses window + closes; returns instance; swallows constructor errors and logs them.
   - `showCriticalAlert`: 🚨/❌/⚠️/ℹ️/🔔 emoji map; `requireInteraction: true` only for `critical`; `tag: alert-${name}`; no-ops when permission not granted.
   - `showTradeNotification`: 📈/📉 based on side; body format check; tag `trade-${token_id}`.
   - Used a `FakeNotification` class installed on both `globalThis` and `window` (so `'Notification' in window` returns true).

6. **Created `src/hooks/useNotifications.test.ts`** — 22 tests covering initial state, enable/disable, toggle, persistence, and polling:
   - Initial state: `supported=true, permission=default, enabled=false` (with Notification mocked).
   - `supported=false` when Notification is absent.
   - `enable()`: requests permission, flips enabled, persists localStorage, fires test toast; stays disabled on denied; skips `requestPermission` when already granted.
   - `disable()`: flips enabled=false, persists 'false'.
   - `toggle()`: delegates correctly.
   - Persistence: re-enables on mount when localStorage flag is `'true'` AND permission granted; does NOT re-enable when permission is default; does NOT re-enable when flag is `'false'`.
   - Polling: no polls when disabled; polls every 30 s when enabled (via `vi.useFakeTimers` + `advanceTimersByTimeAsync`); no polls when tab hidden.
   - New-alert detection: critical alert triggers 🚨 toast with `requireInteraction: true`; error alert triggers ❌ toast with `requireInteraction: false`; info/warning alerts do NOT trigger toasts but DO update seen-set.
   - Seen-set dedup: re-arriving alert does NOT re-toast.
   - 50-entry cap on seen-set (60 alerts do not crash).
   - Error resilience: fetch rejects → no crash; non-200 → no crash; missing `alerts` field → no crash.
   - Used a `mockRequestPermissionResult(value)` helper that ALSO updates `FakeNotification.permission` to match (mirrors real browser behavior where the static permission field auto-syncs after the user responds).

7. **Verification** —
   - `bun run lint` — clean (eslint . exits 0).
   - `bun run test` — all 340 tests pass (46 new + 294 existing). One flaky run had CommandPalette tests fail due to a pre-existing jsdom `scrollIntoView` issue unrelated to my changes; re-running passed clean.
   - `bunx tsc --noEmit` — my files have zero TS errors (other agents' PnLBarChart.tsx has pre-existing recharts type errors that predate this task).

## Stage Summary
- Created `src/lib/notifications.ts` (84 lines) — Web Notifications API primitives.
- Created `src/hooks/useNotifications.ts` (137 lines) — permission lifecycle + 30 s alert-poll React hook.
- Modified `src/components/TopStatusBar.tsx` — added 3-state notification bell toggle button (with full ARIA + tooltip support).
- Created `src/lib/notifications.test.ts` (24 tests, 291 lines).
- Created `src/hooks/useNotifications.test.ts` (22 tests, 477 lines).
- Total new tests: 46.
- Lint: clean. Tests: all 340 pass.

## Files Touched
- `src/lib/notifications.ts` (new)
- `src/hooks/useNotifications.ts` (new)
- `src/components/TopStatusBar.tsx` (modified — added import + bell button)
- `src/lib/notifications.test.ts` (new)
- `src/hooks/useNotifications.test.ts` (new)
