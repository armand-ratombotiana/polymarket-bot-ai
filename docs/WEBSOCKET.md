# WebSocket Real-Time Push Architecture

This document describes the real-time push layer that drives the
Polymarket Pro Trading Workstation's live updates. It covers the
frontend hook contract (`useWebSocket` + `useRealtimeData`), the
message format the backend broadcasts, the reconnection strategy, and
how the system degrades gracefully to polling when the WebSocket is
unavailable.

---

## 1. Overview

The workstation displays ~37 panels of live trading data: positions,
orders, trades, equity, ML metrics, market books, alerts. Driving all
of that with REST polling would mean:

- 30+ panels × 0.5 req/s ≈ **15 req/s** of redundant traffic against
  the FastAPI backend, even when nothing has changed.
- Per-panel jitter as each panel's `setInterval` fires independently.
- Backend rate-limit budget (120/min per IP) consumed in <10s.

The real-time push layer replaces this with a single multiplexed
WebSocket connection to the bot's `/ws` endpoint. The server pushes
updates as they happen; the client subscribes to channels by filtering
on the `channel` field of each incoming message.

When the WS is healthy, **zero polling traffic** is generated. When the
WS drops, polling resumes automatically. When the tab is hidden, both
the WS and the polling pause — no point burning backend quota on a
hidden tab.

```
                     ┌─────────────────────────────────────────┐
                     │              Browser (SPA)              │
                     │                                         │
                     │   useWebSocket (singleton-ish: one     │
                     │   socket per mounted consumer)          │
                     │      │                                  │
                     │      └─► useRealtimeData                │
                     │             │  REST prefetch            │
                     │             │  + WS channel filter       │
                     │             │  + polling fallback       │
                     │             ▼                            │
                     │      Component state (data, isLoading,  │
                     │      isRealtime, error)                 │
                     └─────────────────┬───────────────────────┘
                                       │  ws :81  /ws?token=...
                                       ▼
                  ┌────────────────────────────────────────────┐
                  │  Caddy Gateway (port 81)                   │
                  │  ?XTransformPort=8080 → ws :8080           │
                  └─────────────────┬──────────────────────────┘
                                    │
                                    ▼
                  ┌────────────────────────────────────────────┐
                  │  FastAPI / uvicorn (port 8080)             │
                  │  GET /ws → WebSocket upgrade                │
                  │  broadcast loop pushes snapshots every ~1s  │
                  │  + on-demand for fills / kill-switch       │
                  └────────────────────────────────────────────┘
```

---

## 2. The Two Hooks

### `useWebSocket(options)`

The lowest-level hook — owns one WebSocket connection. Use this when
you need direct access to the socket (e.g. sending commands, listening
to multiple channels, custom reconnect logic).

```ts
import { useWebSocket } from '@/hooks/useWebSocket'

const { isConnected, lastMessage, send, disconnect } = useWebSocket({
  onMessage: (msg) => console.log('got:', msg),
  onConnect: () => console.log('connected'),
  onDisconnect: () => console.log('disconnected'),
  reconnectInterval: 3000,       // default
  maxReconnectAttempts: 10,     // default
})
```

Contract:
- **Connects on mount.** `new WebSocket(getAuthedWsUrl())` is called
  inside a `useEffect([], [connect])`. The URL carries the auth token
  (`?token=...`) and the gateway port (`?XTransformPort=8080`) so
  Caddy can route the upgrade to the FastAPI process.
- **Reconnects on close.** Up to `maxReconnectAttempts` times, spaced
  `reconnectInterval` ms apart. The counter resets to 0 on every
  successful `onopen`.
- **Pauses on tab-hidden.** The visibilitychange listener calls
  `wsRef.current?.close()` when `document.hidden` becomes true, and
  reconnects immediately when the tab becomes visible again (without
  waiting for the next reconnect-interval tick).
- **Cleans up on unmount.** The cleanup function flips
  `shouldReconnect.current = false` BEFORE calling `close()`, so the
  `onclose` handler sees the flag and skips the reconnect
  `setTimeout`.

### `useRealtimeData(endpoint, options)`

The hook most panels should use — composes `useWebSocket` with a REST
prefetch and a polling fallback.

```ts
import { useRealtimeData } from '@/hooks/useRealtimeData'

const { data, isLoading, error, isRealtime } = useRealtimeData<Position[]>(
  '/api/positions',
  {
    wsChannel: 'positions',     // subscribe to WS channel
    pollInterval: 10_000,       // fallback poll every 10s
    initialData: [],            // seed before first fetch resolves
  },
)
```

Contract:
- **REST prefetch on mount.** Fires one `apiFetch(endpoint)` to
  populate the initial state synchronously — no flash of empty content
  while the WS handshake completes.
- **WS subscription.** If `wsChannel` is set, incoming WS messages
  whose `msg.channel === wsChannel` replace `data` with `msg.data`.
- **Polling fallback.** When `isConnected === false`, an interval
  polls `endpoint` every `pollInterval` ms. The interval tears down
  the moment the WS reconnects (the effect deps on `isConnected`).
- **Tab-hidden suppression.** Each poll tick checks
  `document.hidden` and skips the fetch if the tab is not visible.
- **Returns `{ data, isLoading, error, isRealtime }`.** `isRealtime`
  is `true` only when the WS is connected — components can show a
  "live" indicator based on this flag.

---

## 3. Message Format

Every message broadcast on the WebSocket is a JSON object with at least
these two fields:

```jsonc
{
  "channel": "positions",       // subscription channel name
  "data": { ... },              // channel-specific payload
  "timestamp": 1698765432100   // optional, ms since epoch
}
```

The client never has to introspect the payload shape to route a
message — it filters purely on `channel`. The payload schema is a
contract between the broadcasting service and the consuming hook
(see §4 below for the canonical channel list).

### Why a single socket + channel filter (not per-channel sockets)?

1. **Connection efficiency.** A single TCP/TLS connection serves every
   panel — the backend's broadcast loop only multiplexes one writer
   per client instead of N.
2. **Backpressure simplicity.** If the WS drops, every channel stops
   simultaneously and the polling fallback kicks in uniformly.
3. **Auth once.** The token is in the URL query on the upgrade
   request; per-channel subscriptions don't need to re-auth.
4. **Trivial client impl.** `if (msg.channel === wsChannel) setData(msg.data)`
   — no subscription protocol, no ACK round-trips.

---

## 4. Available Channels

| Channel     | Payload                          | Pushed when                              |
|-------------|----------------------------------|------------------------------------------|
| `positions` | `Position[]` (full array)       | Any position opens, closes, or marks.   |
| `orders`    | `Order[]` (open orders)          | Order placed, cancelled, or filled.     |
| `trades`    | `Trade[]` (recent trades)        | A new trade executes (paper or live).    |
| `metrics`   | `BotSnapshot` (full snapshot)    | Status, daily PnL, kill-switch flips.    |
| `alerts`    | `{ level, message, ts }`         | Risk threshold breached, kill-switch on.|

Channels map 1:1 to existing REST endpoints:

| Channel     | REST endpoint       |
|-------------|---------------------|
| `positions` | `/api/positions`    |
| `orders`    | `/api/orders`       |
| `trades`    | `/api/trades`       |
| `metrics`   | `/api/snapshot`     |
| `alerts`    | `/api/events`       |

A consumer subscribing to `positions` over the WS gets the exact same
payload shape it would have received from `GET /api/positions`, so the
REST prefetch and the WS update can share a single `data` slot
without transformation.

---

## 5. Reconnection Strategy

```
open ──► close (network glitch)
            │
            ▼
        attempt #1 (after reconnectInterval = 3s)
            │
            ├──► open → SUCCESS, counter resets to 0
            │
            └──► close (still down)
                    │
                    ▼
                attempt #2 (after 3s)
                    │
                    ├──► ... (continues up to maxReconnectAttempts = 10)
                    │
                    └──► give up → polling fallback takes over indefinitely
```

- **Reconnect interval.** Fixed at 3s by default. We don't use
  exponential backoff because the WS almost always recovers within
  one or two attempts (the bot restart is fast), and a 30s+ backoff
  window would make the UI feel dead during recovery.
- **Max attempts.** 10 by default. After 10 failures (~30s of
  continuous downtime), the hook stops trying — `isRealtime` stays
  false, and the polling fallback keeps the UI live until the user
  reloads the page.
- **Counter reset.** Any successful `onopen` resets the attempt
  counter to 0, so a flaky connection that recovers briefly doesn't
  exhaust the budget.
- **Manual disconnect.** `disconnect()` flips `shouldReconnect = false`
  first, so the subsequent `close()` doesn't trigger a reconnect
  setTimeout.
- **Tab-hidden pause.** The visibilitychange listener closes the
  socket immediately when the tab is hidden (freeing the server-side
  connection) and reconnects the instant the tab becomes visible
  again, bypassing the reconnect-interval delay.

---

## 6. Fallback Polling

`useRealtimeData` runs a `setInterval` that fires `apiFetch(endpoint)`
every `pollInterval` ms (default 10s) — but only when the WS is NOT
connected. The moment the WS reconnects, the polling effect's
cleanup function clears the interval.

```ts
useEffect(() => {
  if (isConnected) return                       // WS live → no polling
  if (document.hidden) return                   // tab hidden → skip
  const interval = setInterval(async () => {
    if (document.hidden) return                 // hidden mid-tick → skip
    const res = await apiFetch(endpoint)
    if (res.ok) setData(await res.json())
  }, pollInterval)
  return () => clearInterval(interval)
}, [endpoint, isConnected, pollInterval])
```

Notes:
- The interval deps include `isConnected`, so a WS reconnect re-runs
  the effect and tears down the interval.
- `document.hidden` is checked both in the deps (skip starting the
  interval at all) and inside the tick callback (skip a tick that
  fires after the tab is hidden mid-interval). This is belt-and-braces
  — the inner check handles the case where the tab was visible when
  the interval started but becomes hidden before the first tick.
- Poll errors are swallowed silently. A persistent failure will
  eventually flip `error` via the initial-fetch effect, but the
  background poll keeps retrying without surfacing errors to the user
  (the UI is expected to keep showing the last-known-good state).

---

## 7. Usage Examples

### A. Migrate a polling panel to real-time

Before (ad-hoc polling):

```tsx
function PositionsPanel() {
  const [positions, setPositions] = useState<Position[]>([])
  useEffect(() => {
    const poll = () => apiFetch('/api/positions')
      .then(r => r.json())
      .then(d => setPositions(d.positions))
    poll()
    const id = setInterval(poll, 2000)
    return () => clearInterval(id)
  }, [])
  return <Table data={positions} />
}
```

After (real-time + polling fallback):

```tsx
function PositionsPanel() {
  const { data, isLoading, isRealtime } = useRealtimeData<{ positions: Position[] }>(
    '/api/positions',
    { wsChannel: 'positions', pollInterval: 10_000 },
  )
  if (isLoading) return <Skeleton />
  return (
    <>
      <Badge variant={isRealtime ? 'live' : 'idle'}>
        {isRealtime ? 'LIVE' : 'POLLING'}
      </Badge>
      <Table data={data?.positions ?? []} />
    </>
  )
}
```

### B. Multiple panels share one WS via `useWebSocket`

If several sibling panels need to listen to different channels and
don't want three separate REST prefetches, they can share a parent
`useWebSocket` and dispatch via a context:

```tsx
const WSContext = createContext<{ lastMessage: any }>({ lastMessage: null })

function Dashboard() {
  const { lastMessage, isConnected } = useWebSocket()
  return (
    <WSContext.Provider value={{ lastMessage }}>
      <ConnectionBadge isConnected={isConnected} />
      <PositionsPanel />
      <OrdersPanel />
      <TradesPanel />
    </WSContext.Provider>
  )
}

function PositionsPanel() {
  const { lastMessage } = useContext(WSContext)
  const [positions, setPositions] = useState<Position[]>([])
  useEffect(() => {
    if (lastMessage?.channel === 'positions') {
      setPositions(lastMessage.data)
    }
  }, [lastMessage])
  // ...
}
```

### C. Send a command back to the server

```tsx
const { send } = useWebSocket()
// ...
<button onClick={() => send({ action: 'cancel_all_orders' })}>
  Cancel All
</button>
```

(The backend currently ignores inbound messages from the dashboard —
all mutations go through REST endpoints like `DELETE /api/orders`. The
`send()` API is included for future bidirectional use cases such as
subscribing to a subset of channels.)

---

## 8. Testing Strategy

The hooks are unit-tested with a `MockWebSocket` class installed on
`global.WebSocket` so tests can imperatively drive `open` / `message` /
`close` transitions:

```ts
class MockWebSocket {
  static instances: MockWebSocket[] = []
  // ...
  triggerOpen() { this.readyState = OPEN; this.onopen?.() }
  triggerMessage(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }) }
  triggerClose() { this.readyState = CLOSED; this.onclose?.() }
}
```

Test coverage:

| File                          | Tests                                                                                              |
|-------------------------------|----------------------------------------------------------------------------------------------------|
| `useWebSocket.test.ts`         | Mount-time connect, open/message/close handlers, JSON parse failure, reconnect + cap + counter reset, disconnect + unmount cleanup, send() gating, tab-hidden pause, tab-visible resume. |
| `useRealtimeData.test.ts`     | Initial REST fetch (success / HTTP error / network throw), WS-channel override, channel mismatch ignore, polling fallback when WS down, polling teardown on WS connect, tab-hidden tick suppression, initialData seed, isRealtime flag transitions. |

Run the suite:

```bash
bun run test            # full vitest suite
bun run test -- useWebSocket   # one hook only
```

---

## 9. Future Work

- **Channel subscription protocol.** Currently the server pushes every
  channel to every connected client. For high-frequency channels
  (e.g. raw order-book deltas), a subscribe/unsubscribe protocol
  would let the client opt in to only the channels it renders.
- **Per-message backpressure.** If the client falls behind (e.g.
  during a heavy render), messages pile up in the browser's WS event
  queue. A high-watermark + coalescing strategy (drop intermediate
  `positions` messages, keep only the latest) would smooth this.
- **Server-sent events fallback.** For environments where WS upgrades
  are blocked (corporate proxies, etc.), an SSE fallback would be
  more compatible than long-polling. Not needed today — the polling
  fallback already covers this case adequately.
- **Migrate `useBot`.** The legacy `useBot.ts` hook still drives the
  main dashboard via 2s REST polling with its own inline WS handling.
  A future wave should migrate it to compose `useRealtimeData` so the
  whole frontend converges on one real-time layer.
