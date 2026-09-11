# W23-4 — Real-time Alert Notifications via WebSocket

**Agent:** full-stack-developer
**Task ID:** W23-4
**Date:** 2026-09-04
**Scope:** Additive — 1 new hook (`useAlertNotifications.ts`) + 1 new
component (`AlertNotificationsPanel.tsx`) + 2 new test files (43 tests
total) + 1 surgical edit to `TopStatusBar.tsx` to mount the panel.

## What was done

### Step 1 — `src/hooks/useAlertNotifications.ts`

A thin React hook that composes the existing W11-4 `useWebSocket` to
subscribe to the `alerts` channel and forward incoming alert
payloads to both a rolling in-memory list (capped at 50, most-recent
first) and the W13-6 `showCriticalAlert` desktop toast helper.

Surface:
```typescript
const {
  alerts,           // Alert[] — most-recent first, capped at 50
  unreadCount,      // number — incremented per alert, decremented per ack
  enabled,          // boolean — local mute toggle for OS toasts
  isConnected,      // boolean — pass-through from useWebSocket
  acknowledge,      // (alertId: string) => void — removes one + decrements
  acknowledgeAll,   // () => void — clears both lists
  toggle,           // () => void — flips enabled (does NOT revoke permission)
} = useAlertNotifications()
```

Design notes (captured in the file header):
- `onMessage` callback is wrapped in `useCallback([enabled])` — but
  `useWebSocket` stores `onMessage` in a ref that is refreshed on
  every render (no deps array on the ref-sync effect), so the closure
  swap doesn't tear down the socket. The `useCallback` is purely for
  React reconciliation cheapness.
- `enabled` is a **local** mute toggle, distinct from the browser's
  notification permission. When `enabled=false`, the hook still
  records alerts (so the in-app feed stays complete) but suppresses
  the OS toast. This mirrors the W13-6 `useNotifications` semantics:
  the trader can mute the audio interruption without losing the
  visual feed.
- The hook filters strictly on `channel === 'alerts'` AND
  `data.type === 'alert'` AND `data.alert.alert_id` being a string.
  Other WS frames (positions, orders, snapshot, alerts/snapshot
  meta-frames) are ignored so the hook composes cleanly with the
  other consumers of the shared `useWebSocket`.
- `acknowledge(id)` for an unknown id is a no-op (preserves list +
  counter); `acknowledge(id)` for a known id removes it and
  decrements `unreadCount`, clamped at 0. `acknowledgeAll()` clears
  both atomically.
- The list cap (50) is enforced via `[alert, ...prev].slice(0, 50)`.
  The `unreadCount` is NOT capped — the trader should still see
  "you missed 60 alerts" if they walked away, even if the list only
  retains the most-recent 50.

### Step 2 — `src/components/AlertNotificationsPanel.tsx`

A Radix Popover that renders the bell trigger in the TopStatusBar and
a dropdown panel listing recent alerts. Composed entirely from
shadcn/ui primitives (`Popover`, `PopoverTrigger`, `PopoverContent`,
`Button`) + the new `useAlertNotifications` hook.

Visual language matches the workstation's dark theme
(`#0e1015` panel surface, `#1f2335` borders, `#dde1ed` primary text,
`#7e8aaa` secondary text) so the bell trigger slots in next to the
existing status-bar pills without visual clash. Severity colours
follow the W13-6 conventions already used by `showCriticalAlert`:

| Severity  | Icon | Dot colour   | Border-left | Text colour      |
| --------- | ---- | ------------ | ----------- | ---------------- |
| critical  | 🚨   | `bg-red-400`    | red-500     | `text-red-400`     |
| error     | ❌   | `bg-orange-400` | orange-500  | `text-orange-400`  |
| warning   | ⚠️   | `bg-amber-400`  | amber-500   | `text-amber-300`   |
| info      | ℹ️   | `bg-blue-400`   | blue-500    | `text-blue-400`    |

Features implemented (all required by the W23-4 spec):
1. **Bell icon trigger** — inline SVG (no icon font dependency),
   inherits `currentColor` so it adapts to the parent's hover state.
   `aria-label="Alerts, N unread"` includes the count for screen
   readers; `aria-haspopup="dialog"` + `aria-expanded` for proper
   Popover semantics.
2. **Unread count badge** — absolute-positioned at the top-right of
   the bell, only rendered when `unreadCount > 0`. Clamps at "99+"
   to avoid overflow on long alert bursts. Red background with a
   subtle shadow for visibility against the dark status bar.
3. **Dropdown panel** — 360px wide, capped at 360px tall with
   `overflow-y-auto` and a thin scrollbar. Renders the empty state
   (`🔔 No active alerts. New alerts will appear here in real time.`)
   when there are no alerts; otherwise renders each alert as a
   `<button>` row (keyboard-focusable, click-to-acknowledge) with
   the severity dot, icon, name, message, relative timestamp
   (`5s ago`, `3m ago`, `2h ago`, `1d ago`), and severity label.
4. **Live indicator** — small pill in the panel header showing
   `Live` (green dot, pulsing) when `isConnected=true` and
   `Polling` (amber dot) when `isConnected=false`. Mirrors the
   visual language of the W15-5 `ConnectionStatusPill`.
5. **Mute toggle** — 🔔/🔕 button in the panel header that flips
   the hook's local `enabled` flag. `aria-pressed={enabled}` for
   toggle-button semantics. Does NOT revoke the browser
   notification permission (impossible from JS); the user must do
   that via the browser's site-settings panel.
6. **Acknowledge All button** — footer action that calls
   `acknowledgeAll()`. Only rendered when there are alerts (so the
   user can't trigger a no-op). Disabled-style fallback is handled
   by the conditional render.
7. **Footer alert count** — `N active alerts · M unread` summary
   with singular/plural handling (`1 active alert` vs
   `2 active alerts`). Unread count is highlighted red so the
   trader can see at a glance how many are new since the last
   acknowledgement.

Accessibility:
- The bell trigger's `aria-label` includes the unread count so
  screen readers announce "Alerts, 3 unread".
- The dropdown is a Radix Popover (`role="dialog"`) so ESC +
  outside-click dismiss it by default.
- Each alert row is a `<button>` (not a `<div>`) — keyboard
  focusable, Enter/Space activates the acknowledge.
- The mute toggle has `aria-pressed` + a descriptive
  `aria-label` that flips between "Mute desktop alert
  notifications" / "Enable desktop alert notifications".
- The "Acknowledge All" button has `aria-label="Acknowledge all
  alerts"` (the visible "✓ Acknowledge All" text is also
  accessible).

### Step 3 — `src/components/TopStatusBar.tsx` wiring

Surgical 2-edit change to mount the panel:
1. Added `import { AlertNotificationsPanel } from './AlertNotificationsPanel'`
   alongside the existing component imports, with a W23-4 comment
   explaining the relationship to W13-6 `useNotifications`
   (complementary, not a replacement — W13-6 catches alerts that
   arrive between WS pushes via 30s polling; W23-4 catches the live
   push as it happens via WS).
2. Inserted `<AlertNotificationsPanel />` between `<LocaleSwitcher />`
   and the audio mute toggle button. This clusters the two
   "alert-related" controls together (bell = visual feed,
   mute = audio cue) so the trader can find both quickly.

No other props or state changes to `TopStatusBar` — the panel is
self-contained (it composes its own `useAlertNotifications` hook,
which composes its own `useWebSocket`).

### Step 4 — Tests

#### `src/hooks/useAlertNotifications.test.ts` (23 tests)

Uses the same MockWebSocket shim as `useWebSocket.test.ts` /
`ConnectionStatus.test.tsx`, plus the same FakeNotification class as
`useNotifications.test.ts`. 8 test groups covering:

- **Initial state** — empty alerts, zero unread, enabled=true,
  not connected on mount.
- **WebSocket message capture** — alerts channel + type='alert'
  + valid alert_id adds to list most-recent-first; other channels
  ignored; non-`alert` types ignored; missing alert_id ignored;
  list caps at 50 with FIFO eviction; unread counter grows
  beyond 50 (only the list is capped, not the counter).
- **acknowledge()** — removes one + decrements; no-op for unknown
  id; never underflows below zero.
- **acknowledgeAll()** — clears both; no-op when already empty.
- **toggle()** — flips enabled; does NOT revoke permission.
- **isConnected** — pass-through from useWebSocket; reflects open
  and close events.
- **Desktop toast side-effects** — fires `new Notification(...)` via
  `showCriticalAlert` when enabled AND permission granted; does
  NOT fire when enabled=false (but alert is still recorded);
  does NOT fire when permission is default (but alert is still
  recorded); fires per alert in a sequence; does NOT crash when
  the Notifications API is unavailable.
- **Closure stability across re-renders** — flipping enabled on
  then off then on resumes the toast firing; the alert feed is
  continuous throughout (toast gating is independent of feed
  recording).

#### `src/components/AlertNotificationsPanel.test.tsx` (20 tests)

Mocks `useAlertNotifications` via `vi.mock` so the component tests
stay focused on rendering/interaction contracts (the hook has its
own comprehensive coverage). 5 test groups covering:

- **Trigger button** — renders without crashing; no badge when
  unreadCount=0; numeric badge with correct count; "99+" clamp
  for >99; aria-label includes count; no "unread" suffix when
  zero.
- **Empty state** — empty-state copy rendered when no alerts;
  Acknowledge All button NOT rendered when no alerts.
- **Live indicator** — `Live` text + green dot class when
  isConnected=true; `Polling` text + amber dot class when
  isConnected=false.
- **Alerts list rendering** — name + message + severity label
  per row; colour-codes each severity (red/orange/amber/blue
  dot classes via innerHTML contains); footer count with
  singular/plural.
- **Interactions** — clicking a row calls `acknowledge(id)`;
  clicking "Acknowledge All" calls `acknowledgeAll()`; mute
  toggle calls `toggle()`; mute icon flips 🔔/🔕; ESC closes the
  popover.

### Verification

- `cd /home/z/my-project && bunx eslint src/hooks/useAlertNotifications.ts
  src/hooks/useAlertNotifications.test.ts
  src/components/AlertNotificationsPanel.tsx
  src/components/AlertNotificationsPanel.test.tsx
  src/components/TopStatusBar.tsx` → **clean (no errors, no warnings)**.
  The repo-wide `bun run lint` reports 12 pre-existing errors in
  `src/components/StrategyPerformancePanel.tsx` (a file this task
  did NOT touch — it's a separate concurrent agent's staged file).
  My new + modified files all pass ESLint cleanly.
- `cd /home/z/my-project && bunx vitest run
  ./src/hooks/useAlertNotifications.test.ts
  ./src/components/AlertNotificationsPanel.test.tsx` →
  **43 passed (43)** across 2 test files in 28.14s.
- `cd /home/z/my-project && bunx vitest run --maxWorkers=4` →
  **924 passed (924)** across 43 test files in 174.29s. No
  regressions — the new 43 tests are net additions to the suite.
- Dev server log (`dev.log`) — clean. Next.js 16.1.3 / Turbopack
  compiled `Ready in 5.5s`; no runtime errors introduced by the
  new panel or TopStatusBar mount.

## Stage Summary

- Created 1 new hook (`useAlertNotifications.ts`, 102 lines)
  composing `useWebSocket` (W11-4) + `showCriticalAlert` (W13-6).
- Created 1 new component (`AlertNotificationsPanel.tsx`, ~260
  lines) composing the hook + Radix Popover + shadcn/ui Button
  with all 7 spec features (bell trigger, unread badge,
  dropdown panel, severity colour-coding, Live indicator,
  mute toggle, Acknowledge All).
- Wired the panel into `TopStatusBar.tsx` via a 2-edit surgical
  change (1 import + 1 element placement) between the LocaleSwitcher
  and the audio mute toggle so the two alert-related controls
  cluster together.
- Created 2 new test files (43 tests total): 23 hook tests +
  20 component tests, covering all required contract surfaces
  (WS message capture, alerts added to state, acknowledge +
  acknowledgeAll + toggle, panel rendering with/without alerts,
  unread count badge, acknowledge button).
- Lint clean for all new + modified files; 43 new tests pass;
  full 924-test suite passes with no regressions.

## Known limitations / follow-ups

1. **Server-side acknowledge not yet wired.** The hook's
   `acknowledge(id)` and `acknowledgeAll()` are purely client-side
   — they remove the alert from the local list but do NOT call
   `POST /api/alerts/:id/ack` (that endpoint doesn't exist yet).
   The trader who re-mounts the workstation will see the same
   alerts again. W23-5 is the planned follow-up to wire the
   server-side ack.
2. **No persistence.** The alert list lives in component state only
   — refreshing the page clears it. This is intentional for the
   initial W23-4 scope (the spec asked for real-time push, not
   history); a localStorage or IndexedDB cache can be layered on
   in a future wave if the trader needs to see alerts that arrived
   while the tab was closed.
3. **Composes a second `useWebSocket` instance.** The panel mounts
   its own `useWebSocket` (via `useAlertNotifications`), so the
   workstation now has at least three WS hooks running
   concurrently: `ConnectionStatusPill`, `useRealtimeData` (for
   the data panels), and now `useAlertNotifications`. Each opens
   its own socket — that's three server-side connections per tab.
   The W11-4 design doc explicitly chose this over a shared socket
   for isolation; a future wave could promote a single
   `useWebSocketProvider` if connection count becomes a concern.
4. **Bell icon is an inline SVG, not a Lucide icon.** The project
   has `lucide-react` available, but the existing TopStatusBar
   icons (gear, mute, shortcuts, etc.) all use emoji glyphs, so an
   inline SVG bell keeps the visual weight consistent with the
   surrounding icon cluster. If a future design pass migrates the
   status-bar icons to Lucide, the bell should go with them.
