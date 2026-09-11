# W11-8 — full-stack-developer — PWA support (service worker, manifest, offline)

**Task ID:** W11-8
**Agent:** full-stack-developer
**Date:** 2026-09-04
**Task:** Add Progressive Web App (PWA) support to the Polymarket Pro dashboard — web app manifest, app icons, service worker for offline caching, SW registration, and an offline banner component. 12 unit tests.

## Inputs read
- `/home/z/my-project/worklog.md` (tail ~200 lines) — established Wave 10 context (rate limiting, error boundaries, CI/CD, Docker, Zod, alerts, UI polish). Wave 11 in flight: the worklog tail doesn't yet mention Wave 11 sub-tasks, but the working tree already shows sibling Wave 11 agents' untracked work (`src/hooks/useRealtimeData.ts`, `src/hooks/useWebSocket.ts`, `e2e/`, `playwright.config.ts`, `mini-services/polymarket-bot/core/security.py`, `mini-services/polymarket-bot/ml/calibration.py`, `mini-services/polymarket-bot/core/cache.py`, `docs/WEBSOCKET.md`).
- `/home/z/my-project/src/app/layout.tsx` (42 lines pre-edit) — root layout with metadata + viewport exports, ErrorBoundary wrapper, skip-link. No PWA metadata was present. Imports `./globals.css`.
- `/home/z/my-project/next.config.ts` (12 lines) — `output: "standalone"`, `reactStrictMode: false`, `typescript.ignoreBuildErrors: true`. No PWA-related config; `public/` is served by Next's default static-asset pipeline (no special headers config needed).
- `/home/z/my-project/package.json` (103 lines) — `next@^16.1.1`, `react@^19.0.0`, `vitest@^4.1.11`, `jsdom@^30.0.1`, `@testing-library/react@^16.3.3`. No PWA library installed (workbox, next-pwa) — went with the hand-rolled SW approach per the task spec (no new dependencies needed).
- `/home/z/my-project/public/` — only `logo.svg` (the existing animated Z.ai brand mark) and `robots.txt`. No manifest, no app icons, no service worker.
- `/home/z/my-project/vitest.config.ts` — `environment: 'jsdom'`, `setupFiles: ['./src/test/setup.ts']`. Default test pattern picks up any `*.test.{ts,tsx}` or `*.spec.{ts,tsx}`.
- `/home/z/my-project/src/test/setup.ts` — global `fetch` mock + `matchMedia` mock + `ResizeObserver` mock. Did not need modification — `navigator.onLine` is a property (not a method) and `navigator.serviceWorker` is undefined in jsdom (the helper's `in` guard short-circuits).
- `/home/z/my-project/src/components/AnalyticsPanel.test.tsx` (283 lines) — used as the canonical test-file reference pattern (describe/it/expect vi blocks, jsdom fetch mocking, `act()` for unmount).

## Files created

### `public/manifest.json` (28 lines)
- Validated via `python3 -c "import json; json.load(open('public/manifest.json'))"` — parses cleanly.
- 3 icon entries: `/icon.svg` (`sizes: "any"`, `type: "image/svg+xml"`) for modern browsers that support SVG icons + the 2 PNG entries (`/icon-192.png`, `/icon-512.png`) from the task spec. The PNG files don't exist on disk yet (the spec said "we can't easily generate PNG icons"), but the manifest references them so that when an art-team asset drop happens later the icons will "just work" — modern Chrome will fall back to `/icon.svg` for the install prompt in the meantime.
- `display: "standalone"` (no browser chrome when launched from the home screen), `background_color: "#0b0e14"` + `theme_color: "#0b0e14"` matching the dashboard's dark shell, `orientation: "any"`, `categories: ["finance", "productivity"]`, empty `screenshots: []` array.

### `public/icon.svg` (7 lines)
- 512×512 SVG: dark `#0b0e14` rounded-rect background (`rx=96` for iOS-maskable corner radius), blue (`#3b82f6`) concentric circles + crosshair lines. The blue matches the dashboard's existing primary accent (the dashboard CSS uses `--accent-blue: #3b82f6` in several places) rather than introducing a new brand colour.
- Single-file SVG works as both `apple-touch-icon` and `manifest.icon` — modern Chromium browsers render SVG icons at arbitrary DPR.

### `public/sw.js` (96 lines)
- Hand-rolled service worker (no workbox dependency). `CACHE_NAME = "polymarket-pro-v1"` — bump the version suffix when shipping a new app shell so the `activate` handler evicts the prior cache.
- App shell precache list: `["/", "/manifest.json", "/icon.svg"]`. Next.js's Turbopack dev server emits hashed asset URLs for `/static/` and `/_next/static/` — those are NOT precached because their names change every build; the `fetch` handler caches them on first GET (cache-first strategy).
- `install` handler: `event.waitUntil(caches.open(...).addAll(APP_SHELL))` + `self.skipWaiting()` so the new SW activates immediately on the next navigation rather than waiting for every open tab to close.
- `activate` handler: deletes any caches whose name doesn't match `CACHE_NAME` (so old versions are evicted on upgrade), then `self.clients.claim()` so the new SW takes over its opener tab immediately.
- `fetch` handler:
  1. Non-GET requests (POST/PUT/DELETE) → fall through to network untouched (mutations must reach the API).
  2. `/api/` requests → fall through to network untouched (real-time trading data must NEVER be served from cache).
  3. Same-origin GET requests → cache-first; if cached, return cached; else fetch from network, cache the 200 response, return it. Opaque (CORS) responses (`type: "opaque"`, `status: 0`) are NOT cached.
  4. Document navigation that fails AND has no cache hit → fall back to cached `"/"` so the user sees the dashboard shell (which will then show the OfflineIndicator banner once React hydrates).

### `src/lib/registerSW.ts` (60 lines)
- `'use client'` directive at the top so the module is only ever evaluated in the browser; the `typeof window === 'undefined'` guard makes it a no-op on the server even if accidentally imported from a server component.
- Three defensive exits:
  1. `typeof window === 'undefined'` → SSR no-op (return without touching navigator).
  2. `!('serviceWorker' in navigator)` → old-browser / sandboxed-iframe no-op.
  3. The `register()` call is wrapped in `try/catch` — some sandboxed environments (like the sandbox preview iframe) report `'serviceWorker' in navigator === true` but `.register()` throws synchronously with a SecurityError. We log to `console.error` and don't rethrow — the app still works online-only, which is strictly better than crashing the React mount that called us.
- Deferred to `window.addEventListener('load', register)` if `document.readyState !== 'complete'`, so the SW registration never contends with first-paint critical-path fetches. If the page is already loaded (rare during HMR in dev), register immediately.
- Uses `console.debug` for the success log (silenced by default in browser devtools) and `console.error` for failures (visible by default — surfaces real PWA install failures).

### `src/components/SWRegister.tsx` (22 lines)
- Tiny client-only React wrapper that calls `registerServiceWorker()` exactly once in a `useEffect([])`. Mounting this in the root layout ensures the SW is registered for every route without needing to thread a `useEffect` through every page component.
- Renders `null` — no visual footprint.

### `src/components/OfflineIndicator.tsx` (87 lines)
- `'use client'` directive — uses `useState` + `useEffect`.
- Initial state `false` (online) to avoid SSR/client hydration mismatch (the server renders `null`, the client renders `null` until the `useEffect` runs and syncs the real `navigator.onLine` value).
- `useEffect` mounts `online` + `offline` window-event listeners and syncs `navigator.onLine` on mount (covers the case where the user opened the PWA while already offline).
- Returns `null` when `!isOffline` — no DOM footprint.
- When offline: renders a sticky top banner with `role="status"` + `aria-live="polite"` + `aria-atomic="true"` (so screen readers announce the state change without stealing focus) + `data-testid="offline-indicator"` (so the test file can target it without coupling to the visible message text).
- Styling: dark amber (`#7c2d12` bg, `#fed7aa` text, `#9a3412` border) — distinguishes the offline state from the dashboard's normal blue/green/red status colours so the trader's eye is drawn to it. Inline styles so the component is self-contained (doesn't depend on `globals.css`).

### `src/lib/registerSW.test.ts` (147 lines, 6 tests, all pass)
1. `does not throw when called in a browser that has SW support` — happy path with a stub `navigator.serviceWorker.register` that resolves.
2. `returns early (no throw) when window is undefined (SSR)` — `delete globalThis.window`, assert no throw, restore in `finally`.
3. `returns early (no throw) when navigator.serviceWorker is missing` — replace `navigator` with `{}` (no `serviceWorker` key), assert no throw.
4. `checks for serviceWorker support before calling register` — Proxy with BOTH `get` AND `has` traps (because `'serviceWorker' in navigator` triggers the `has` trap, not `get`). Asserts the helper actually performed the presence check before bailing.
5. `swallows registration errors instead of throwing` — `register` stub that throws synchronously (mimicking SecurityError in sandboxed iframes); asserts the helper's try/catch handles it without re-throwing, AND `console.error` is called (so the failure is visible, not silently swallowed).
6. `registers on the window "load" event when document is not yet complete` — stubs `document.readyState = "loading"`, asserts the helper defers registration to `window.addEventListener("load", ...)` instead of calling `register()` immediately.

### `src/components/OfflineIndicator.test.tsx` (110 lines, 6 tests, all pass)
1. `renders nothing when online (default jsdom state)` — `setNavigatorOnLine(true)` (jsdom's default), assert `container.firstChild === null`.
2. `renders the banner when navigator.onLine is false on mount` — `setNavigatorOnLine(false)`, assert the banner renders with `role="status"` + `aria-live="polite"`.
3. `shows the banner when the browser fires the offline event` — start online, render, dispatch `new Event("offline")`, assert banner appears.
4. `hides the banner when the browser fires the online event` — start offline, render, dispatch `new Event("online")`, assert banner disappears.
5. `includes a user-visible message explaining what happened` — assert `/you are offline/i` matches the banner text.
6. `removes its event listeners on unmount (no leaked setState warnings)` — unmount, then dispatch `offline` event, assert no `act()` warning / throw (proves the cleanup function in the `useEffect` removed the listeners).
- Helper `setNavigatorOnLine(value)` uses `Object.defineProperty(globalThis.navigator, 'onLine', { get: () => value })` because jsdom hardcodes `onLine: true` as a non-writable data property; the per-test defineProperty override is restored in `afterEach`.

## Files modified

### `src/app/layout.tsx`
- Added 2 imports at the top of the file (with W11-8 comment headers): `SWRegister` from `@/components/SWRegister`, `OfflineIndicator` from `@/components/OfflineIndicator`.
- Extended the `metadata` export with: `manifest: "/manifest.json"`, `appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "PolymarketPro" }`, `icons: { icon: "/icon.svg", apple: "/icon.svg" }`.
  - **Intentional deviation from the task spec**: the spec snippet put `themeColor: "#0b0e14"` on `Metadata`. In Next.js 14+ the `themeColor` field was moved from `Metadata` to `Viewport` (deprecation warning fires if set on `Metadata`). Set it on the `viewport` export instead, with an inline comment explaining why.
- Extended the `viewport` export with `themeColor: "#0b0e14"` (so Android Chrome's address bar tints to match the dashboard's dark shell).
- In `<head>`: added 3 explicit link tags (`<link rel="manifest" href="/manifest.json" />`, `<link rel="apple-touch-icon" href="/icon.svg" />`, `<meta name="theme-color" content="#0b0e14" />`). These duplicate what Next.js's Metadata API emits, but make the PWA tags visible in `view-source:` for browsers/crawlers that only read raw `<link>` tags.
- In `<body>`: inserted `<OfflineIndicator />` above the `<ErrorBoundary>` (so the offline banner is visible even when the dashboard has crashed to the error fallback) and `<SWRegister />` after the `<ErrorBoundary>` (so SW registration is deferred until after first paint, matching the spirit of the `load` event deferral in `registerServiceWorker`).

## Verification

### `bun run lint` — CLEAN ✅
- 0 errors, 0 warnings (after fixing 2 `Unused eslint-disable directive` warnings on the `console.debug` / `console.error` calls — `next/core-web-vitals` doesn't enable `no-console`, so the disable directives were redundant).
- NOTE: a separate pre-existing error in `src/hooks/useWebSocket.ts` ("Cannot access variable before it is declared" — `connect` referenced in a `setTimeout` before its own `useCallback` declaration) was visible during the first lint run, then disappeared on the second run. That file is untracked in git (`??` in `git status`) — it's sibling W11-7 subagent work-in-progress, not introduced by W11-8. No code in W11-8 touches that file.

### `bun run test` — 12 new tests PASS ✅
- `src/lib/registerSW.test.ts`: 6/6 pass.
- `src/components/OfflineIndicator.test.tsx`: 6/6 pass.
- Total new tests: 12. Both new test files pass in isolation (`bun run test -- src/lib/registerSW.test.ts src/components/OfflineIndicator.test.tsx` → 12/12 pass, 0 fail).

### Pre-existing test failures NOT introduced by W11-8
- 3 failures in `src/hooks/useRealtimeData.test.ts` (`falls back to polling when the WS is not connected`, `stops polling once the WS connects`, `skips poll ticks while the tab is hidden`) — file is untracked (`??` in `git status`), created by the concurrent W11-7 websocket/realtime subagent. Verified pre-existing: `git stash` (which only stashes tracked files — leaves untracked test files in place) and re-run still produces the same 3 failures, so they are NOT caused by W11-8's layout.tsx change. The `useRealtimeData` hook tests render via `renderHook`, not the full `<RootLayout>`, so the `<SWRegister />` / `<OfflineIndicator />` mount is not even invoked.
- 3 failures in `e2e/*.spec.ts` (`api-health`, `dashboard`, `navigation`) — vitest picks up Playwright spec files because `vitest.config.ts` doesn't exclude `e2e/**` from the default `**/*.{test,spec}.{ts,tsx}` pattern. Pre-existing — those files are untracked (`??` in `git status`), added by a sibling Wave 11 e2e subagent. To fix, either: (a) add `exclude: ['e2e/**']` to `vitest.config.ts`, or (b) run playwright tests via `bunx playwright test` separately and reserve `bun run test` for vitest unit/integration tests. Left as-is — fixing vitest config would modify another subagent's domain.

### `manifest.json` JSON validity ✅
- `python3 -c "import json; d=json.load(open('public/manifest.json')); assert d['name'] and d['short_name'] and d['start_url']; assert len(d['icons']) >= 2; assert d['icons'][0]['src'].endswith('.svg') or d['icons'][0]['src'].endswith('.png')"` → `manifest.json OK: PolymarketPro ( 3 icons, theme: #0b0e14 )`.

### Dev server health ✅
- `tail dev.log` after the layout.tsx edit: no new errors, no compile failures. Next.js's Turbopack dev server hot-reloads `layout.tsx` edits without a manual restart.
- Could not `curl http://localhost:3000/manifest.json` from this shell (sandbox network isolation — `000` connection refused), but the dev.log shows successful `GET / 200` responses, so the public/ assets are served via Next.js's standard static-asset pipeline.

## Design decisions / tradeoffs

1. **No PWA library (no next-pwa, no workbox)** — the task spec provided a complete hand-rolled `sw.js`. Adding `next-pwa` would have introduced a webpack plugin that doesn't compose with Next 16's Turbopack, and `workbox` would have added ~30 KB of runtime for functionality (precaching, runtime caching, background sync) that the dashboard doesn't yet need. The hand-rolled SW is 96 lines and covers the 3 actual requirements: offline app shell, network-first for API, cache-version eviction on deploy.

2. **SVG icons only** — the task spec said "we can't easily generate PNG icons", so the manifest references `/icon.svg` as the primary icon (modern Chrome, Edge, Firefox, and Safari 17+ all support SVG icons) and ALSO references `/icon-192.png` + `/icon-512.png` so when an art team drops the PNGs the icons "just work" without a manifest edit. The SVG has `purpose: "any maskable"` so it adapts to Android's adaptive-icon padding.

3. **Inline styles on OfflineIndicator** — the banner uses inline `style={{...}}` instead of CSS classes so the component is fully self-contained (no dependency on `globals.css` having a specific class defined). This is intentional — the W10-8 motion work added Framer Motion `<FadeIn>` wrappers that would also work, but a sticky offline banner shouldn't fade in (the trader needs to see the offline state instantly).

4. **SW registration deferred to `load` event** — even though `useEffect` already runs after first paint, we further defer the actual `navigator.serviceWorker.register()` call to the browser's `load` event so it never contends with the dashboard's initial data fetches (which fire on mount). This matches the W3C PWA best-practice recommendation.

5. **Cache `type === 'basic' || 'default'` only** — opaque responses (CORS cross-origin fetches to `clob.polymarket.com`) are NOT cached. The dashboard's Polymarket CLOB API calls are cross-origin and wouldn't be cacheable anyway (the SW's `fetch` handler only intercepts same-origin requests thanks to the SW's scope), but the type check is belt-and-braces against accidentally caching an opaque error response.

6. **No themeColor on `Metadata`** — task spec snippet had it there; Next.js 14+ moved it to `Viewport`. Set it on `viewport` and added a comment in `metadata` explaining the move. This avoids the deprecation warning that ESLint would otherwise emit.

## Stage Summary
- Created `public/manifest.json` (28 lines, 3 icons, valid JSON).
- Created `public/icon.svg` (7 lines, 512×512 SVG, blue-on-dark).
- Created `public/sw.js` (96 lines, hand-rolled service worker: install/activate/fetch handlers).
- Created `src/lib/registerSW.ts` (60 lines, client-safe SW registration helper with try/catch + SSR guard + missing-API guard).
- Created `src/components/SWRegister.tsx` (22 lines, useEffect wrapper rendered in layout).
- Created `src/components/OfflineIndicator.tsx` (87 lines, online/offline event-driven sticky banner).
- Created `src/lib/registerSW.test.ts` (6 tests, all pass).
- Created `src/components/OfflineIndicator.test.tsx` (6 tests, all pass).
- Modified `src/app/layout.tsx` (added PWA metadata + viewport.themeColor + 3 link/meta tags + `<OfflineIndicator />` + `<SWRegister />`).
- Tests: 12 new (6 registerSW + 6 OfflineIndicator), all pass.
- Lint: clean (0 errors, 0 warnings on W11-8 files).
- `manifest.json` validates as JSON via `python3 -c json.load`.
- Pre-existing failures documented: 3 in `src/hooks/useRealtimeData.test.ts` (sibling W11-7 work), 3 in `e2e/*.spec.ts` (sibling Wave 11 e2e work — vitest config doesn't exclude Playwright specs). Neither is caused by W11-8.
