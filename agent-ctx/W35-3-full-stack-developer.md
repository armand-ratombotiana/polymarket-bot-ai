# W35-3 — Real-time WebSocket updates for IngestionHealthPanel

**Agent:** full-stack-developer
**Task ID:** W35-3
**Scope:** EDITOR — migrate `IngestionHealthPanel.tsx` from 15 s REST polling of `/api/ingestion/health` to the hybrid `useRealtimeData` hook (REST prefetch + WebSocket push on the `system` channel + 15 s polling fallback). Add live throughput sparkline, live error feed tape, and Live/Polling badge. Update tests.

## Goal
Add real-time WebSocket updates to the IngestionHealthPanel so the operator sees ingestion health changes the moment the backend pushes them (instead of waiting up to 15 s for the next poll). Add three new UI affordances:
1. Live throughput sparkline — appends an EPS sample every time a new health payload arrives.
2. Live error feed — scrolling tape of recent ingestion errors (trade-tape pattern).
3. Live/Polling badge — reflects whether the panel is receiving real-time WS updates or falling back to polling.

## Plan
1. Read context: existing `IngestionHealthPanel.tsx`, `useRealtimeData.ts`, `useWebSocket.ts`, plus the OrdersPanel test for the MockWebSocket test stub pattern.
2. Migrate `/api/ingestion/health` from manual REST polling (inside `fetchAll`) to `useRealtimeData('/api/ingestion/health', { wsChannel: 'system', pollInterval: 15000 })`. Keep the other 4 endpoints (quality, dead-letter, coverage, gaps) on the existing 15 s REST poll.
3. Add `liveEPSHistory` state + sampler effect that appends one sample per new health payload (REST or WS).
4. Add `errorFeed` state + dedupe-ref collector effect that prepends new dead-letter items as they arrive.
5. Replace the static `15s poll` badge with a dynamic `● Live` / `⟳ Polling` badge driven by `isRealtime`.
6. Add Live Throughput + Live Error Feed SectionCards.
7. Update `IngestionHealthPanel.test.tsx` — install MockWebSocket stub, replace the 15s-poll badge test, add 10 new tests covering real-time updates, badge transitions, live throughput sparkline, and live error feed population / prepend / empty / dedupe.
8. Verify lint + tests pass; append worklog.md entry.

## Result

### Component — `src/components/IngestionHealthPanel.tsx`
- Added `useRealtimeData` import + `useRef` to React import list.
- Added `LiveErrorEvent` interface (mirrors dead-letter recent item shape) + 3 constants: `LIVE_EPS_MAX_SAMPLES = 30`, `LIVE_ERROR_FEED_MAX_ROWS = 50`, `INGESTION_WS_CHANNEL = 'system'`.
- Removed local `health` useState; the panel now derives it from `useRealtimeData`'s `data` field.
- Added `liveEPSHistory: number[]` + `errorFeed: LiveErrorEvent[]` + `seenErrorIdsRef: Set<string>` (dedupe ref).
- Refactored `fetchAll` to fetch only the 4 remaining endpoints (quality, dead-letter, coverage, gaps) — health is now owned by `useRealtimeData`.
- Added live throughput sampler effect: fires on every new `healthData` payload, appends `Σ(sources.events_per_second)` to `liveEPSHistory`, caps at `LIVE_EPS_MAX_SAMPLES`.
- Added live error feed collector effect: walks `deadLetter.recent`, prepends any item whose `id` isn't in `seenErrorIdsRef` to `errorFeed`, caps at `LIVE_ERROR_FEED_MAX_ROWS`.
- Updated loading render condition to `(loading || healthLoading) && !health` so the panel doesn't flash empty between fetchAll resolving and useRealtimeData's initial fetch landing.
- Combined `error ?? healthError` into `combinedError` so a WS / health-fetch failure surfaces alongside other-endpoint failures in the same banner.
- Replaced static `15s poll` badge with dynamic `● Live` (Badge variant="success") / `⟳ Polling` (Badge variant="warning") badge driven by `isRealtime`. Same visual convention as OrdersPanel.tsx.
- Added Live Throughput SectionCard: `Sparkline` over `liveEPSHistory` with `Radio` icon (green when live, amber when polling), badge shows `<n>/<max> samples · ws|poll · Σ EPS`, body shows min/max/last sample values, empty state shows spinner + "Waiting for first health snapshot…".
- Added Live Error Feed SectionCard: `role="log"` scrolling list (`max-h-64 overflow-y-auto scrollbar-thin`) of error events. Each row: relative timestamp, source badge, truncated error message, optional retry count. Empty state shows green check + "No ingestion errors observed yet."

### Tests — `src/components/IngestionHealthPanel.test.tsx`
- Added `MockWebSocket` stub class (same shape as OrdersPanel.test.tsx / useRealtimeData.test.ts) + `beforeEach`/`afterEach` pair that installs/restores it on `global.WebSocket`. Required because `useRealtimeData` runs `useWebSocket` on every mount, which calls `new WebSocket(...)` — without the stub, jsdom throws `ReferenceError: WebSocket is not defined` and the useWebSocket catch block schedules reconnect backoff timers.
- Replaced the "15s poll" badge test with a "Polling" badge test (WS not connected → badge shows `⟳ Polling`, `realtime-badge` is absent).
- Added 10 new tests:

| # | Test |
|---|---|
| 1 | renders the "Polling" badge in the header when the WS is not connected |
| 2 | flips the badge to "Live" when the WS connects |
| 3 | flips back to "Polling" when the WS disconnects |
| 4 | updates the rendered total-events KPI when a new health payload arrives over the WS |
| 5 | ignores WS messages on channels other than "system" |
| 6 | renders the live throughput sparkline with a sample after the first health payload |
| 7 | appends a new sample to the live throughput sparkline when a WS push arrives |
| 8 | renders the live error feed with recent dead-letter items |
| 9 | prepends new dead-letter items to the live error feed as they arrive |
| 10 | renders the empty state when the dead-letter queue is empty |
| 11 | does NOT duplicate feed entries when the same dead-letter snapshot arrives twice |

Two of the new tests ("prepends new dead-letter items…" and "does NOT duplicate feed entries…") use `vi.useFakeTimers()` + `vi.advanceTimersByTimeAsync(15_000)` to drive the 15 s poll cycle. They use direct `expect(...)` assertions after the advance instead of `waitFor(...)` because `waitFor` polls on real timers by default and would time out under fake timers.

### Verification
- `cd /home/z/my-project && bun run lint` — clean (`eslint .` exits 0, no warnings, no errors).
- `bun run test` — **64 test files / 1246 tests, all passing**. The IngestionHealthPanel.test.tsx suite grew from 28 → 39 tests (+11 net: 1 modified badge test + 10 new real-time / live-throughput / live-error-feed tests). All 64 test files in the project still pass.

### Files touched

| Path | Change |
|---|---|
| `src/components/IngestionHealthPanel.tsx` | Migrated `/api/ingestion/health` from manual REST polling to `useRealtimeData` (WS channel `system`); refactored `fetchAll` to fetch only the 4 remaining endpoints; added `liveEPSHistory` state + sampler effect; added `errorFeed` state + dedupe-ref collector effect; replaced static `15s poll` badge with Live/Polling badge; added Live Throughput sparkline SectionCard; added Live Error Feed SectionCard; combined `error` + `healthError` into `combinedError` for the loading + banner conditions. |
| `src/components/IngestionHealthPanel.test.tsx` | Added MockWebSocket stub + `beforeEach`/`afterEach` install/restore; replaced the "15s poll" badge test with a "Polling" badge test; added 10 new tests covering Live/Polling badge transitions, real-time WS updates, channel filtering, live throughput sparkline sampling, live error feed population / prepend / empty / dedupe. |

### Notes / trade-offs
1. **Only the health endpoint migrated to WS.** The W35-3 spec specifically calls out `useRealtimeData('/api/ingestion/health', { wsChannel: 'system', pollInterval: 15000 })`. The other 4 ingestion endpoints (quality, dead-letter, coverage, gaps) stay on the existing visibility-aware 15 s REST poll via `fetchAll`. Migrating them too would require either 4 separate WS channels (the backend doesn't currently expose them) or a multiplexed channel — out of scope.
2. **Live throughput sparkline is panel-owned, not backend-owned.** The backend's `metrics.throughput_trend` is a server-side sample buffer that ships with the health payload; the new "Live Throughput" sparkline is a *client-side* buffer that appends one sample every time a new health payload arrives (REST or WS). The two cards are intentionally distinct: the backend trend shows the bot's view of EPS over its sampling window; the live trend shows the operator's view of how often the panel itself is receiving updates.
3. **Error feed dedupes by dead-letter item id.** The `seenErrorIdsRef` ref persists across renders so the same DLQ snapshot arriving via REST poll (which returns the N most-recent items every cycle) doesn't re-populate the tape every 15 s.
4. **MockWebSocket installed globally for all tests.** The `beforeEach` installs the stub unconditionally because `useRealtimeData` runs `useWebSocket` on every mount — even tests that don't drive the WS need the stub to avoid the `ReferenceError` thrown by `new WebSocket(getAuthedWsUrl())` in jsdom.
5. **No sidebar / page.tsx / route wiring changes.** W35-3 extends the existing IngestionHealthPanel (already wired by W31-5); no new route or nav item is needed.
