# W14-7 — Rate Limit Dashboard + Analytics

**Agent**: full-stack-developer
**Task ID**: W14-7
**Date**: 2025

## Summary

Added a Rate Limit analytics dashboard panel that surfaces in-memory
rate-limit-hit data from the FastAPI backend. The panel polls
`GET /api/rate-limit/stats` every 30s and renders KPI cards, a bar
chart of hits-by-endpoint, a sparkline of hits-per-minute, and two
tables (top endpoints, top clients) plus a policy reference card.

## Files Created / Modified

### Backend (Python)
- **CREATED** `mini-services/polymarket-bot/core/rate_limit_tracker.py` (255 lines)
  - `RateLimitHit` dataclass + `RateLimitTracker` class
  - `deque(maxlen=1000)` for bounded memory
  - `threading.Lock` for thread-safe mutation
  - `record_hit()` + `record_request()` mutators
  - `get_stats()` returns 6-key dashboard shape
  - Module-level singleton `rate_limit_tracker`
- **MODIFIED** `mini-services/polymarket-bot/api/server.py`
  - Added import of `rate_limit_tracker`
  - `rate_limit_handler`: calls `record_hit(...)` before returning 429
  - `request_logging_middleware`: calls `record_request(...)` for both
    success path and 500-path
  - New route: `GET /api/rate-limit/stats` (tags=["system"], rate-limited
    by READ_LIMIT)
- **CREATED** `mini-services/polymarket-bot/tests/test_rate_limit_tracker.py`
  (21 tests, 350 lines)
  - TestRecordHit, TestGetStats, TestRecordRequest, TestThreadSafety,
    TestReset, TestSingleton

### Frontend (TypeScript/React)
- **CREATED** `src/components/RateLimitPanel.tsx` (480 lines)
  - 4 KPI cards (Total Hits, Hit Rate, Top Endpoint, Top Client)
  - PnLBarChart for hits-by-endpoint
  - Sparkline for hits-per-minute (60m window)
  - Top Rate-Limited Endpoints table
  - Top Rate-Limited Clients table
  - Most-Requested Endpoints table (all-requests view)
  - Rate-Limit Policy reference section (6 policy cards)
  - 30s visibility-aware polling
  - Loading skeleton + empty state + hard-error retry
- **MODIFIED** `src/components/Sidebar.tsx`
  - Added `'system-rate-limit'` to `NavSection` union
  - Added nav item in `system` group with `nav.rate_limits` labelKey
- **MODIFIED** `src/messages/en.json` + `src/messages/fr.json`
  - Added `nav.rate_limits` (en: "Rate Limits", fr: "Limites Taux")
  - Added previously-missing `nav.audit` key (W14-4 dependency)
- **MODIFIED** `src/app/page.tsx`
  - Added `lazyPanel(() => import('@/components/RateLimitPanel'), 'Loading Rate Limits…')`
  - Added `{activeSection === 'system-rate-limit' && ...}` render case
    with `<PanelErrorBoundary label="Rate Limits">` wrapper
- **CREATED** `src/components/RateLimitPanel.test.tsx` (18 tests, 380 lines)
  - Loading state, KPI rendering, formatting, empty state, hard-error
    state, polling (30s interval), visibility-aware pause, unmount
    cleanup, manual Refresh button, Authorization header propagation

## Verification Results

- `bun run lint`: clean (exit 0)
- `bun run test`: 459/459 tests pass (18 new RateLimitPanel tests + 441 existing)
- `python -m pytest tests/test_rate_limit_tracker.py -v`: 21/21 tests pass in 0.72s
- Backend smoke test confirms `/api/rate-limit/stats` route is registered

## Architecture Notes

The `RateLimitTracker` is intentionally a separate module from
`core.prometheus_metrics`:

- `prometheus_metrics.rate_limit_hits_total` is a single monotonic
  counter keyed on `endpoint` — appropriate for Grafana scraping but
  lacks the per-IP / per-limit / per-minute shape the dashboard renders.
- Adding per-IP as a prometheus label would balloon cardinality
  (one time series per distinct client IP — a known footgun).
- A small in-memory tracker with a bounded deque + defaultdict is the
  right tool for a "last hour, top-N" view where a process restart is
  an acceptable freshness boundary.

The `record_request()` method keys its counter as
`f"{endpoint}:{status}"` while `record_hit()` keys as the raw endpoint.
The `get_stats()` method's `top_endpoints` field filters out keys
containing `:` so the dashboard's "Most-Requested Endpoints" table
reflects rate-limit-hit volume specifically — not raw traffic. The
raw-traffic counts (keyed by `endpoint:status`) remain in
`_request_counts` for future panels that may want to surface 4xx vs
2xx separately.

## Pattern References

- Polling + visibility-aware pause pattern matches `ObservabilityPanel.tsx`
- Recharts mock pattern (passthrough `ResponsiveContainer`) matches
  `charts/Charts.test.tsx`
- Fake-timer pattern (`vi.useFakeTimers()` + `act(async () => await
  vi.advanceTimersByTimeAsync(N))`) matches `hooks/useNotifications.test.ts`
- `lazyPanel()` + `<PanelErrorBoundary>` + `<FadeIn>` wrapper pattern
  matches the existing W8-10 system panels
