# W11-4 — full-stack-developer — WebSocket real-time push

## Task summary
Added a WebSocket real-time push layer to reduce polling load on the
FastAPI backend. Created two reusable hooks (`useWebSocket` +
`useRealtimeData`), their unit tests (27 tests total), and
documentation (`docs/WEBSOCKET.md`). Existing components were NOT
modified — they'll be migrated to the new hooks in a future wave.

## Files created
- `src/hooks/useWebSocket.ts` (139 lines) — generic WS hook with
  auto-reconnect, visibility-aware pause/resume, ref-based callback
  storage so callers can pass new closures without tearing down the
  socket.
- `src/hooks/useRealtimeData.ts` (115 lines) — hybrid hook: REST
  prefetch on mount, WS subscription via channel filter, polling
  fallback when WS is down, tab-hidden tick suppression.
- `src/hooks/useWebSocket.test.ts` (16 tests, all passing) — mount-
  time connect, open/message/close handlers, JSON parse failure,
  reconnect interval + cap + counter reset, explicit disconnect,
  unmount cleanup, send() gating, tab-hidden pause, tab-visible
  resume.
- `src/hooks/useRealtimeData.test.ts` (11 tests, all passing) —
  initial REST fetch (success / HTTP error / network throw),
  WS-channel override, channel mismatch ignore, polling fallback,
  polling teardown on WS connect, tab-hidden tick suppression,
  initialData seed, isRealtime flag transitions.
- `docs/WEBSOCKET.md` (335 lines) — architecture overview, two-hook
  contract, message format, 5 canonical channels (positions / orders
  / trades / metrics / alerts) mapped 1:1 to existing REST endpoints,
  reconnection strategy, fallback polling, usage examples, testing
  strategy, future work.

## Files modified
- `worklog.md` — appended the W11-4 stage entry (this wave's record).
- (none else — the task forbade modifying existing components)

## Key design decisions

### Refs for callbacks (not state)
The WebSocket event handlers (`onopen`, `onmessage`, `onclose`) read
from refs (`onMessageRef`, `onConnectRef`, `onDisconnectRef`) that
are refreshed on every render. This lets callers pass new closures
without forcing a socket reconnect — critical because both the mount
effect and the visibilitychange effect depend on `connect`, and an
unstable `connect` would re-run them (re-creating the socket +
listener) on every parent render.

### `connect` memoisation
`connect` is `useCallback([reconnectInterval, maxReconnectAttempts])`
— only those two numeric config values can change its identity.
Callback swaps don't affect it (they go through refs).

### `shouldReconnect` is a ref, not state
Flipped synchronously in the cleanup function BEFORE `close()` is
called, so the `onclose` handler sees the flag and skips the
reconnect `setTimeout`. State would be async and race the close.

### Hybrid REST + WS in `useRealtimeData`
1. On mount, fire `apiFetch(endpoint)` to populate state
   synchronously — no flash of empty content while the WS handshake
   completes.
2. WS messages with `msg.channel === wsChannel` override `data`.
3. When `isConnected === false`, poll `endpoint` every
   `pollInterval` ms (default 10s). The interval tears down the
   moment the WS reconnects (effect deps on `isConnected`).
4. Each poll tick checks `document.hidden` and skips if true.

## Testing gotcha: fake timers + waitFor
The initial draft of `useRealtimeData.test.ts` used
`vi.useFakeTimers()` + `waitFor(...)` for the polling tests. This
FAILED because `waitFor` from `@testing-library/dom` uses
`setInterval(check, 50)` internally, which fake timers pause — so
`waitFor` never ticks its polling check and hangs until the 5s test
timeout.

Fix: switched to REAL timers with a short `pollInterval: 100` ms,
and used `await new Promise(r => setTimeout(r, 350))` to wait for
≥3 poll ticks to have fired if the interval were active. Documented
the rationale in a comment in the test file.

This is a generalisable lesson: **don't mix `vi.useFakeTimers()`
with `waitFor`** — they're fundamentally incompatible because
`waitFor` depends on `setInterval` ticking. For hook tests that
need to assert state after async operations + fake timers, either
flush microtasks manually via `await act(async () => {})` or use
real timers with short intervals.

## Verification
- `bun run lint` — clean (eslint exit 0, no warnings).
- `bun run test` — 207 passed, 0 failed, 0 errors across 9 test files.
- Dev server (`bun run dev`) — still healthy, `/` renders 200 in 28ms.

## Pre-existing test failures observed (NOT caused by W11-4)
On the FIRST test run, vitest briefly picked up the Playwright spec
files in `e2e/*.spec.ts` (which use `test.beforeEach` from
`@playwright/test`, incompatible with vitest's runner). On subsequent
runs vitest's default exclude (`e2e/**`) kicked in and these files
were correctly skipped. This is a pre-existing config quirk unrelated
to W11-4's changes — no source/config files were modified to address
it.

## Future waves
- Migrate `useBot.ts` from its inline WS handling + 2s REST poll to
  compose `useRealtimeData` so the main dashboard converges on the
  same real-time layer as the panels.
- Migrate individual panels (PositionsPanel, OrdersPanel, TradesPanel,
  AnalyticsPanel, MLPanel, etc.) from their ad-hoc
  `useEffect + setInterval` polling patterns to `useRealtimeData`
  with the appropriate `wsChannel`.
- Optionally add a channel subscription protocol so the client can
  opt in to only the channels it renders (reduces bandwidth for
  high-frequency channels like raw order-book deltas).
