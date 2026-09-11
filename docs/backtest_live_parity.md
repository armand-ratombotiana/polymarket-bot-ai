# Backtest / Live Parity Contract

**Scope.** This document defines the parity contract between the
polymarket-bot's three execution venues — `BacktestBroker`,
`PaperBroker`, and `LiveBroker`. It enumerates what is **guaranteed
identical** across venues, what **may legitimately differ**, how the
parity contract is **maintained** in the codebase, and how it is
**tested** by the parity suite at
`mini-services/polymarket-bot/tests/test_parity.py`.

**Source of truth.** The structural fix lives in
`mini-services/polymarket-bot/core/broker.py` (the `Broker` ABC + the
three concrete subclasses). The contract was introduced in W19-7 to
close the §32 God Mode finding: prior to W19-7 the backtest engine and
the paper/live broker shared zero code — the backtest walked a
synthetic 5-level CLOB book with `spread_bps` + `depth_decay` +
square-root market impact while paper/live used tick-based crossing +
size + queue. Two paths, two slippage models, no parity contract. W19-7
unified them on a single canonical slippage static method.

---

## 1. What is guaranteed identical

The parity contract guarantees the following are **bit-equal** across
backtest, paper, and live for the same inputs:

### 1.1. Strategy signal layer

A strategy that implements the `StrategyContract` ABC
(`strategies/base.py`) is **stateless** across venues by construction —
`generate_signal(market_ctx)` is a pure function of the supplied
`market_ctx` dict. The parity contract asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same `market_ctx` dict (token_id, mid, spread, position, capital, …) | Same `Signal` (action, token_id, size, price, confidence, edge, reason, metadata) |

Two fresh strategy instances built from the same config produce
byte-equal `Signal` objects for the same `market_ctx`. Test:
`test_same_signal_for_same_input`.

### 1.2. Risk decision layer

`risk_manager.check_order(order)` is a pure function of the supplied
`Order` and the global risk state (`store.daily_pnl`, `peak_equity`,
per-strategy cooldowns, kill-switch state). The parity contract
asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same `Order` (token_id, side, price, size, strategy, paper) | Same `(allowed: bool, reason: str)` tuple  |

Two consecutive calls with the same `Order` return the same decision.
Test: `test_same_risk_decision_for_same_signal`.

### 1.3. Order intent layer

A strategy derives an `OrderRequest` deterministically from
`(signal, capital, risk_params)` via the `StrategyContract` methods
(`size_position`, `entry_logic`). The parity contract asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same `(signal, capital, risk_params)` triple   | Same `OrderRequest` (token_id, side, size, price, order_type, time_in_force, client_order_id, strategy) |

Test: `test_same_order_intent_for_same_risk_decision`.

### 1.4. Slippage / fill price layer

This is the **load-bearing §32 contract**. Every concrete `Broker`
subclass delegates `apply_slippage(price, size, side, order_book=None)`
to the shared static helper `Broker._canonical_slippage`, which in turn
delegates to `paper.simulator.PaperSimulator._apply_slippage` (the
canonical tick-based slippage model). The model is deterministic:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same `(price, size, side)` + same `order_book` (optional) | Same `(fill_price, fill_size)` tuple      |

The queue-tick term is a stable SHA-256 hash of the synthetic `order_id`,
which is itself derived from `(price, size, side)`. So identical inputs
always produce identical outputs. Tests:
`test_apply_slippage_parity_across_brokers` (parametrized over 6
`(price, size, side)` triples),
`test_backtest_submit_order_uses_canonical_slippage`,
`test_broker_interface_consistency`.

### 1.5. Position accounting layer

`BacktestBroker.submit_order` mirrors `paper/simulator.py::_execute_fill`
accounting: BUY → weighted-average entry price; SELL → realized P&L =
`(exit - entry) * shares_sold`. The parity contract asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same fill sequence (BUY / BUY / SELL / …)      | Same `Position` snapshot (token_id, side, size, avg_price, realized_pnl) |

Tests: `test_same_position_for_same_fills`,
`test_same_pnl_for_same_trades`.

### 1.6. P&L attribution layer

The realized P&L recorded on a closed position by `BacktestBroker`
matches the P&L `PaperBroker` would record (via the paper simulator's
`_execute_fill` accounting) for the same fill sequence. The P&L parity
contract also asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same round-trip trade (BUY then SELL)          | Same realized P&L == same balance delta        |

The `pnl_a == balance_delta_a` assertion in
`test_same_pnl_for_same_trades` is the load-bearing check: the strategy's
reported P&L must equal the bankroll's P&L (balance change), with no
hidden slippage between the two.

### 1.7. Replay determinism

`HistoricalReplayEngine.replay(token_id, strategy, start, end, capital)`
is synchronous and stateless across invocations (the strategy's own
state is reset between runs by constructing a fresh instance). The
parity contract asserts:

| Input                                          | Output                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Same snapshot series + strategy config + window | Same `ReplayResult` (trades, equity_curve, total_return, sharpe, max_drawdown, win_rate, profit_factor) |

Test: `test_deterministic_replay`.

### 1.8. Interface uniformity

Every concrete `Broker` subclass implements the same six
abstractmethod signatures (`submit_order`, `cancel_order`,
`get_order_status`, `get_positions`, `get_balance`, `apply_slippage`)
so a strategy coded against `Broker` works against any venue without
per-mode branching. Test: `test_broker_interface_consistency`.

---

## 2. What may legitimately differ

The parity contract is **per-decision** — same inputs → same outputs.
It is **not** a full-state equivalence. The following are explicitly
allowed to differ between backtest and live:

### 2.1. Fill timing

- `BacktestBroker.submit_order` returns **synchronous** `FILLED` /
  `REJECTED` responses (no async execution venue).
- `PaperBroker.submit_order` returns `ACKNOWLEDGED`; the actual fill
  lands asynchronously via the simulator's 1-second fill loop.
- `LiveBroker.submit_order` returns `ACKNOWLEDGED`; the fill lands via
  the CLOB WebSocket fill stream (the W18 fill-ack follow-up).

A backtest sees the fill immediately; paper/live sees the fill later.
This is **unavoidable** — the parity contract is per-decision, not
per-tick.

### 2.2. Latency

- Backtest: simulated (the replay loop's wall-clock delta is the
  strategy's view of "time between snapshots").
- Paper: bounded by the simulator's 1-second fill loop and the
  strategy's poll cadence.
- Live: unbounded — network round-trip, exchange matching engine
  queue depth, RPC retries.

Latency differences are not parity violations; they're an execution-
quality concern tracked by `core/latency_tracker.py`.

### 2.3. Partial fill availability

- `BacktestBroker`: fills the requested `size` in full (the canonical
  slippage model only adjusts the fill price; size reduction is a
  future partial-fill extension).
- `PaperBroker`: fills the requested `size` in full (same canonical
  model).
- `Live`: the CLOB may **partially fill** — a 100-share BUY might
  fill 60 shares at the top ask and leave 40 shares resting.

This is the most material divergence: a backtest cannot reproduce
live partial fills because the canonical slippage model is
"size-preserving". Mitigations:

- The `BacktestBroker.SELL` path **clamps** the fill size to the open
  position size so a SELL larger than the position doesn't go negative
  (matches the paper simulator's behavior).
- A future W?? extension can introduce a `partial_fill_probability`
  parameter on `Broker.apply_slippage`; until then, parity is over
  **filled-vs-rejected** decisions, not over **fill size**.

### 2.4. Order rejection reasons

The `OrderResponse.error` string is **not** parity-guaranteed —
different venues produce different error messages for the same
underlying rejection. What **is** guaranteed:

| Layer             | Parity guarantee                                         |
| ----------------- | -------------------------------------------------------- |
| `OrderResponse.status` | `FILLED` / `REJECTED` / `ACKNOWLEDGED` parity across venues for the same decision |
| `OrderResponse.error`   | **Not** parity-guaranteed (per-venue error message) |

### 2.5. Live exchange fees + real spread

`LiveBroker.apply_slippage` is an **estimator** — it uses the canonical
tick-based slippage model so the strategy can size positions
identically across venues. The live exchange charges **real fees + real
spread** at fill time; the estimator's job is to give the strategy a
consistent size signal across venues, not to predict the live fill
price exactly.

The parity contract therefore covers:

| Stage                 | Parity guarantee                                       |
| --------------------- | ------------------------------------------------------ |
| Pre-trade estimation | `apply_slippage` is byte-equal across venues           |
| Post-trade fill price | Live fill price may differ from estimated price (real fees + spread + queue dynamics) |

### 2.6. Kill-switch state

The kill switch is durable (file-backed) and process-global — a
backtest cannot activate or deactivate it (the `BacktestBroker` holds
its own capital + positions ledger, so the risk engine's
`store.kill_switch_active` check is the only global coupling).

In practice the parity suite runs with the kill switch cleared (the
`conftest.py` autouse fixture `_reset_store_factory_defaults` removes
the durable marker file before every test), so this divergence is
only relevant in production.

---

## 3. How parity is maintained

### 3.1. Single canonical slippage model

The single source of truth for slippage is the
`paper.simulator.PaperSimulator._apply_slippage` static method. Every
`Broker` subclass delegates to it via the shared
`Broker._canonical_slippage` helper (`core/broker.py:238`). A
concrete subclass that overrides `apply_slippage` with a different
model breaks the parity contract — the test
`test_apply_slippage_parity_across_brokers` surfaces such a regression
immediately.

### 3.2. Hermetic broker state

`BacktestBroker` holds its **own** capital + positions ledger
(`_capital`, `_positions`, `_orders`) — it has zero coupling to the
in-memory `store` singleton. Two `BacktestBroker` instances running in
the same process don't see each other's positions, and a backtest run
doesn't perturb the live / paper broker's view of the world. Test:
`test_no_hidden_state_leakage`.

`PaperBroker` and `LiveBroker` deliberately share state via `store`
(production code paths that import `store` directly must see the same
state), so they are not hermetic. This is by design — the parity
contract is over **per-decision outputs**, not over internal state.

### 3.3. Deterministic strategy interface

The `StrategyContract` ABC (`strategies/base.py:89`) defines 9 sync
methods that every strategy must implement. The contract is
**stateless across calls** — `generate_signal(market_ctx)` reads only
the supplied `market_ctx` (and the strategy's own per-instance state,
which is reset by constructing a fresh instance). A strategy that
depends on global state (e.g. `store.paper_balance`) breaks the
parity contract.

### 3.4. Synchronous replay engine

`HistoricalReplayEngine.replay` is intentionally synchronous (single
pass through the snapshot list — see
`backtesting/historical_replay.py:148`). The strategy's `_mids` deque
is per-instance; constructing a fresh strategy instance per replay
guarantees the rolling-average state is reset, so two replays with the
same inputs produce byte-equal `ReplayResult` outputs. Test:
`test_deterministic_replay`.

### 3.5. Lazy imports

`core/broker.py` uses lazy imports (`from core.data_store import ...`
inside `_canonical_slippage`) so the broker module is importable in
environments where the paper-simulator singleton isn't yet
constructed. This avoids the import-order races that historically broke
parity (e.g. a test that imported `core.broker` before
`paper.simulator` would see a different slippage model than a test
that imported them in the opposite order).

---

## 4. How parity is tested

The parity suite lives at
`mini-services/polymarket-bot/tests/test_parity.py` (16 tests, ~1100
lines). Run it with:

```bash
cd /home/z/my-project/mini-services/polymarket-bot
python -m pytest tests/test_parity.py -v
```

### 4.1. Test inventory

| #  | Test name                                              | Layer              | What it asserts                                                  |
| -- | ------------------------------------------------------ | ------------------ | ---------------------------------------------------------------- |
| 1  | `test_same_signal_for_same_input`                      | Signal             | Two fresh strategy instances produce byte-equal `Signal` for the same `market_ctx`. |
| 1b | `test_same_signal_for_same_input_hold_case`            | Signal             | Two fresh strategy instances both return `None` for a hold-case `market_ctx`. |
| 2  | `test_same_risk_decision_for_same_signal`             | Risk               | The same `Order` returns the same `(allowed, reason)` from `risk_manager.check_order` across three consecutive calls. |
| 3  | `test_same_order_intent_for_same_risk_decision`       | Order intent       | The same `(signal, capital, risk_params)` produces byte-equal `OrderRequest` + byte-equal `apply_slippage` output. |
| 4  | `test_same_position_for_same_fills`                   | Position           | Two `BacktestBroker`s fed identical fill sequences end up with byte-equal `Position` snapshots + balance. |
| 5  | `test_same_pnl_for_same_trades`                       | P&L                | Two `BacktestBroker`s accumulate byte-equal realized P&L; P&L equals balance delta. |
| 6  | `test_no_hidden_state_leakage`                        | Isolation          | A `BacktestBroker` run leaves the global `store` / `paper_sim` singletons byte-equal before / after. |
| 7  | `test_deterministic_replay`                          | Replay             | The same replay inputs produce byte-equal `ReplayResult` (trades, equity_curve, metrics) across two consecutive runs. |
| 8  | `test_broker_interface_consistency`                   | Interface          | All three concrete `Broker` subclasses implement the same six abstractmethods and produce byte-equal `apply_slippage` outputs. |
| 9  | `test_apply_slippage_parity_across_brokers` (×6)      | Slippage           | Parametrized over 6 `(price, size, side)` triples — all three brokers produce byte-equal `(fill_price, fill_size)`. |
| 10 | `test_backtest_submit_order_uses_canonical_slippage`  | Slippage           | `BacktestBroker.submit_order` fill price equals `broker.apply_slippage` expected price — same model. |

### 4.2. Test fixtures

- **`parity_strategy`** — fresh `ParityStrategy` instance. The
  `ParityStrategy` class is defined inside the test module and bypasses
  `BaseStrategy.__init__`'s `settings.paper_trade` coupling so it is
  constructed identically regardless of the live `TRADING_MODE` env var.
- **`market_context_buy`** — a market context dict that triggers a BUY
  signal (mid < entry_threshold).
- **`market_context_hold`** — a market context dict that produces no
  signal (mid between thresholds).
- **`isolated_risk_manager`** — fresh `InstitutionalRiskEngine` from
  `conftest.py` (no per-strategy cooldowns, observation-only mode off)
  so the risk tests don't perturb the global `risk_manager` singleton.

### 4.3. Deterministic test data

- The `market_snapshots` SQLite DB is seeded with a deterministic
  mean-reverting series (25 baseline snapshots at `mid=0.50`, a dip at
  `mid=0.40`, a recovery at `mid=0.50`) so the replay tests see the same
  snapshot series on every run.
- The `apply_slippage` parity matrix parametrizes over 6
  `(price, size, side)` triples covering:
  - mid-price small / large orders
  - low-price / high-price orders
  - near-floor (`0.10`) / near-ceiling (`0.90`) orders that exercise
    the clamping path

### 4.4. Isolation

The parity suite uses the `conftest.py` autouse
`_reset_store_factory_defaults` fixture (resets `store` / `paper_sim` /
`risk_manager` singletons to factory baseline before every test) plus
its own `/tmp/pmbot_parity_tests` env-redirect sandbox so a test that
fails mid-run doesn't leak state into the next. The `MARKET_DB_PATH`
env var is redirected to `/tmp/pmbot_parity_tests/market_intelligence.db`
so the replay tests don't clobber any real persisted state.

---

## 5. Known limitations

### 5.1. Partial fills

The canonical slippage model is **size-preserving** — `fill_size` always
equals the requested `size`. Live CLOB partial fills are not reproduced
by the backtest. See §2.3 above.

### 5.2. Live fill-ack (W18 follow-up)

`LiveBroker.get_order_status` returns `None` — the current
`clob_client` has no per-order `GET /order/{id}` endpoint. A strategy
that needs status polling should use the `store.open_orders` lookup
path via `PaperBroker` / `BacktestBroker` until the W18 fill-ack lands.
This is documented in `core/broker.py:557`.

### 5.3. Live order rejection parity

`LiveBroker.submit_order` returns `REJECTED` when the CLOB client
returns `None` (signing failure, HTTP 4xx/5xx, network error). The
parity suite **mocks** the CLOB client (via `AsyncMock`) so live-
rejection paths are not exercised end-to-end. A future integration
test against a CLOB testnet could close this gap.

### 5.4. Risk engine global-state coupling

`risk_manager.check_order` reads `store.daily_pnl`, `peak_equity`,
`kill_switch_active`, and per-strategy cooldowns — these are
process-global singletons. The parity suite uses the
`isolated_risk_manager` fixture to avoid perturbing the global
singleton, but a production run that activates a kill switch mid-
backtest would diverge from a clean-state backtest. The parity
contract is over **per-decision** outputs given a fixed global state,
not over the global state itself.

---

## 6. Change-management protocol

Any change to one of the following surfaces **requires** a parity
suite update + a parity review:

| Surface                                         | Owner module                              | Parity test that breaks on a divergence                       |
| ----------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------- |
| `Broker.apply_slippage` signature / semantics   | `core/broker.py`                         | `test_apply_slippage_parity_across_brokers`, `test_broker_interface_consistency` |
| `PaperSimulator._apply_slippage` model          | `paper/simulator.py`                      | `test_apply_slippage_parity_across_brokers`, `test_backtest_submit_order_uses_canonical_slippage` |
| `BacktestBroker.submit_order` accounting        | `core/broker.py::BacktestBroker`          | `test_same_position_for_same_fills`, `test_same_pnl_for_same_trades` |
| `paper.simulator._execute_fill` accounting      | `paper/simulator.py::_execute_fill`       | `test_same_pnl_for_same_trades` (P&L = balance delta assertion) |
| `StrategyContract` ABC method set               | `strategies/base.py::StrategyContract`   | `test_broker_interface_consistency` (interface enumeration) |
| `HistoricalReplayEngine.replay` determinism     | `backtesting/historical_replay.py`        | `test_deterministic_replay`                                  |
| `risk_manager.check_order` decision logic       | `risk/manager.py`                         | `test_same_risk_decision_for_same_signal`                    |

**Protocol.** Open a PR with the change + the parity suite update in
the same commit. The CI gate runs the parity suite on every PR; a
regression on any of the tests above blocks the merge until either
the change is reverted or the parity suite is updated to reflect
the new (intentional) divergence — which then requires an update to
this document.
