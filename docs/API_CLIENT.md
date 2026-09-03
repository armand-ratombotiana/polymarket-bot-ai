# API Client (Frontend)

The frontend talks to the FastAPI backend through two complementary layers in
`src/lib/` ([`api.ts`](../src/lib/api.ts) + [`api-client.ts`](../src/lib/api-client.ts)):

1. **`api.ts`** — low-level fetch utilities (gateway routing, auth headers,
   WebSocket URL). One auth-aware fetch path shared by every call site.
2. **`api-client.ts`** — typed namespace SDK (W12-8) wrapping the utilities in
   per-resource objects (`api.system.health`, `api.trading.getPositions`, …)
   so call sites read like English and response shapes are typed end-to-end.

The Caddy gateway (port 81) fronts both Next.js (port 3000) and FastAPI
(port 8080); the client appends `?XTransformPort=8080` to `/api/*` requests so
the gateway routes them to the backend automatically.

## Layer 1 — `src/lib/api.ts` (utilities)

### `getApiUrl(): string`
Returns the API base URL. The empty string is intentional — all requests are
same-origin (the Caddy gateway proxies), so no host prefix is required.

### `getWsUrl(): string`
Returns the WebSocket URL. Browser-side it derives `ws://` or `wss://` from the
page protocol and appends `?XTransformPort=8080`. SSR-safe (returns a
`localhost:8080` default when `window` is undefined).

### `getApiToken(): string`
Reads the bearer token from `localStorage['polymarket_api_token']` (browser) or
`process.env.NEXT_PUBLIC_API_TOKEN` (SSR). Falls back to the shipped dev token.

### `authHeaders(extra?): Record<string, string>`
Builds the request headers, injecting `Authorization: Bearer <token>` when a
token is present. Merges `extra` first so callers can override.

### `getAuthedWsUrl(): string`
Returns `getWsUrl()` with the `token=<…>` query param appended for the
WebSocket auth handshake.

### `apiFetch(input, init?): Promise<Response>`
The single auth-aware fetch entrypoint. Sets `Authorization` on every call
(unless the caller already provided one) and runs the request through the
gateway-port wrapper.

### `withGatewayPort(input): string`
Appends `?XTransformPort=8080` to `/api/*` paths (skips absolute URLs, `wss://`,
`/api/bot`, and already-tagged URLs). Called automatically by `apiFetch` and
the installed fetch wrapper.

### Fetch wrapper (`installFetchWrapper`)
On the client, `api.ts` monkey-patches `window.fetch` once at import time so
that **every** fetch (including ones from third-party libs) transparently gets
the gateway port appended. The original native fetch is preserved on
`nativeFetch.__nativeFetch` so callers can opt out if needed.

## Layer 2 — `src/lib/api-client.ts` (typed namespace SDK)

W12-8 wraps the raw `apiFetch` helper in a frozen namespace map so each backend
route is declared once, in one place, with a typed return. Call sites read like
English and a renamed backend field (e.g. `positions` → `open_positions`) is a
compile error instead of a silent `undefined` in the UI.

### Namespaces (17)

| Namespace        | Sample methods                                            |
| ---------------- | -------------------------------------------------------- |
| `systemApi`      | `health()`, `status()`                                   |
| `tradingApi`     | `getPositions()`, `getOrders()`, `cancelOrder(id)`       |
| `marketsApi`     | `list()`, `getDepth(tokenId)`                            |
| `mlApi`          | `getMetrics()`, `retrain()`                              |
| `analysisApi`    | `getAttribution()`, `getExecutionQuality()`             |
| `riskApi`        | `reconcile()`, `getExposure()`                           |
| `strategiesApi`  | `catalog()`, `toggle(name, enabled)`                     |
| `arbitrageApi`   | `scan()`                                                 |
| `analyticsApi`   | `get()`, `history()`                                    |
| `observabilityApi`| `metrics()`                                             |
| `alertsApi`      | `list()`, `acknowledge(id)`                              |
| `decisionsApi`   | `rejected()`                                             |
| `safetyApi`      | `readiness()`, `enable()`                                |
| `configApi`      | `get()`, `update(patch)`                                 |
| `cacheApi`       | `stats()`, `clear(name?)`                                |
| `flagsApi`       | `list()`, `toggle(name, enabled)`                        |
| `backtestApi`    | `run(config)`                                            |

A master `api` object re-exports them grouped, so call sites can use either
the grouped form (`api.trading.getPositions()`) or import a single namespace
directly (`import { tradingApi }`) for tree-shaking.

### Contract

- Methods return `Promise<T>` and **throw** `ApiError` (with `status` + parsed
  `body`) on failure. The calling hook (`useBot` / TanStack Query) owns the
  try/catch — matches the existing `useBot` pattern.
- POST / PUT / DELETE bodies are JSON-stringified with `Content-Type:
  application/json` set automatically.
- Response types are Zod-inferred from [`schemas.ts`](../src/lib/schemas.ts)
  for contract-critical endpoints; endpoints still being audited are `any`
  with a `// TODO: tighten response type` marker.

### Compatibility

`api-client.ts` is additive — it does **not** replace the existing `apiFetch`
calls in `hooks/useBot.ts` or components. The two patterns coexist: `apiFetch`
for ad-hoc fetches, `api.*` for typed namespace calls. A follow-up migration
task swaps the call sites over incrementally.

## Usage patterns

```ts
// Low-level (api.ts)
import { apiFetch, authHeaders, getAuthedWsUrl } from '@/lib/api'

const res = await apiFetch('/api/positions')
const data = await res.json()

const ws = new WebSocket(getAuthedWsUrl())

// Typed SDK (api-client.ts)
import { api } from '@/lib/api-client'

const positions = await api.trading.getPositions()      // Promise<Position[]>
await api.trading.cancelOrder(orderId)                   // Promise<void>
const metrics = await api.ml.getMetrics()                // Promise<MLMetrics>
```

## Real-time hooks

Two React hooks compose `apiFetch` + `getAuthedWsUrl` to give panels a hybrid
REST-seed + WS-push data flow — see [`WEBSOCKET.md`](WEBSOCKET.md) for the
full contract.

## See also
- [API.md](API.md) — full route reference (request/response shapes, error codes)
- [WEBSOCKET.md](WEBSOCKET.md) — real-time push contract & hooks
- [SECURITY.md](SECURITY.md) — token handling, fail-closed auth
