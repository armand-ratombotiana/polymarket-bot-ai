# Polymarket Trading Bot 🤖

A fully-featured, professional-grade automated trading bot for [Polymarket](https://polymarket.com) built in Python.

## Features

| Feature | Description |
|---|---|
| 📊 **Market Making** | Places bid+ask quotes around mid-price, earns the spread |
| ⚡ **Combinatorial Arb** | Finds YES+NO < $1.00 mispricings and locks in risk-free profit |
| 🧠 **Signal Trading** | Heuristic scoring model for directional limit orders |
| 📡 **Live WebSocket Feed** | Real-time order book data with auto-reconnect |
| 🛡️ **Risk Manager** | Kill-switch, daily loss limit, per-market + total exposure caps |
| 📄 **Paper Trading** | Full simulation mode — no real money, same live data |
| 📺 **Rich Dashboard** | Live terminal UI with markets, positions, orders, event log |

---

## ⚠️ Disclaimer

> Trading bots involve significant financial risk. **Always run in paper mode first.**  
> The authors take no responsibility for financial losses. Use at your own risk.  
> Polymarket is geo-blocked in some jurisdictions (US, UK, etc.). Comply with your local laws.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A Polygon wallet with **pUSD** (Polymarket's collateral token)
- Access to Polymarket from your jurisdiction

### 2. Install dependencies

```bash
cd polymarket-bot
pip install -r requirements.txt
```

### 3. Configure your `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
POLY_PRIVATE_KEY=0xYOUR_WALLET_PRIVATE_KEY

# Leave API keys blank — they'll be derived automatically on first run
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=

# Start in paper mode (HIGHLY RECOMMENDED for testing)
PAPER_TRADE=true
```

### 4. (Optional) Get your API keys manually

Instead of auto-deriving, you can pre-generate keys on the Polymarket website:  
Settings → API Keys → Create New Key

---

## Docker (Recommended — 24/7 Deployment)

The whole stack is containerized: **TimescaleDB/PostgreSQL + bot (FastAPI/supervisord) + Next.js web UI**.

```bash
cp .env.example .env          # configure wallet, keep PAPER_TRADE=true
docker compose --profile paper up -d --build   # or: make up
```

| URL | Service |
|---|---|
| http://localhost:3010 | Web dashboard (Next.js) |
| http://localhost:8080  | Bot REST API + WebSocket (`/api/health`, `/ws`) |
| http://localhost:5432  | TimescaleDB/PostgreSQL |

**Live mode** (real money — run paper mode first for 24h+):

```bash
docker compose --profile live up -d --build    # or: make live
```

Convenience commands via `make`: `build`, `up`, `live`, `down`, `logs`, `cancel`, `status`, `shell-bot`, `shell-ui`, `clean`.

> The paper and live bot profiles are mutually exclusive (both map host port `8080`).  
> The bot reads `DATABASE_URL` and connects to the bundled TimescaleDB; if unavailable it falls back to SQLite in `/app/data`.

---

## Commands

```bash
# List active markets (no credentials needed)
python main.py markets

# Search markets
python main.py markets --search "election"

# Run in paper mode (simulate with live data)
python main.py paper

# Run in live mode (uses PAPER_TRADE setting from .env)
python main.py run

# Force paper mode even if .env says PAPER_TRADE=false
python main.py run --paper

# Show current risk status
python main.py status

# Emergency: cancel ALL open orders
python main.py cancel-all
```

---

## Configuration Reference

All settings live in `.env`. Key parameters:

### Risk Management

| Setting | Default | Description |
|---|---|---|
| `MAX_OPEN_ORDERS` | 20 | Max concurrent open orders |
| `MAX_POSITION_PER_MARKET_USDC` | 100 | Max exposure per market |
| `MAX_TOTAL_EXPOSURE_USDC` | 500 | Total portfolio exposure cap |
| `DAILY_LOSS_LIMIT_USDC` | 50 | Kill-switch triggers at this loss |

### Market Making

| Setting | Default | Description |
|---|---|---|
| `MM_ENABLED` | true | Enable market making |
| `MM_MARKET_TOKEN_IDS` | *(auto)* | Comma-separated YES token IDs |
| `MM_SPREAD_BPS` | 200 | Bid-ask spread in basis points (200 = 2%) |
| `MM_QUOTE_SIZE_USDC` | 10 | Size of each quote in USDC |
| `MM_MAX_INVENTORY_USDC` | 100 | Max YES token inventory |

### Arbitrage Scanner

| Setting | Default | Description |
|---|---|---|
| `ARB_ENABLED` | true | Enable arb scanner |
| `ARB_MIN_PROFIT_BPS` | 50 | Min profit threshold (50 = 0.5%) |
| `ARB_SCAN_INTERVAL_SECONDS` | 30 | How often to scan all markets |
| `ARB_ORDER_SIZE_USDC` | 20 | Size of each arb leg |

### Signal Trader

| Setting | Default | Description |
|---|---|---|
| `SIGNAL_ENABLED` | false | Enable signal trading |
| `SIGNAL_MIN_CONFIDENCE` | 0.65 | Minimum score to place an order |
| `SIGNAL_ORDER_SIZE_USDC` | 10 | Order size |

---

## Architecture

```
polymarket-bot/
├── main.py                  # CLI entry point (typer)
├── config.py                # Pydantic settings (reads .env)
├── core/
│   ├── clob_client.py       # CLOB REST API (L1/L2 auth)
│   ├── gamma_client.py      # Gamma API (market discovery)
│   ├── ws_client.py         # WebSocket feed (real-time order book)
│   └── data_store.py        # In-memory state (orders, positions, P&L)
├── strategies/
│   ├── base.py              # Abstract base strategy
│   ├── market_maker.py      # Market making
│   ├── arb_scanner.py       # Combinatorial arbitrage
│   └── signal_trader.py     # Signal-driven directional trading
├── risk/
│   └── manager.py           # Kill switch, exposure limits
├── paper/
│   └── simulator.py         # Paper trading fill engine
└── dashboard/
    └── app.py               # Rich terminal UI
```

---

## Getting Started (Paper Mode — Recommended)

1. Copy `.env.example` to `.env` — keep `PAPER_TRADE=true`
2. Run `python main.py markets` to confirm API connectivity
3. Run `python main.py paper` — the dashboard will appear with live market data and simulated orders
4. Watch the Event Log panel for trades and arb alerts
5. When you're satisfied with the behavior, set `PAPER_TRADE=false` to go live

---

## Extending the Signal Trader

To plug in an LLM or external data feed, subclass `SignalTraderStrategy` and override `signal_score()`:

```python
from strategies.signal_trader import SignalTraderStrategy
from core.data_store import OrderBook, Side

class MyAIStrategy(SignalTraderStrategy):
    name = "my_ai"
    
    def signal_score(self, mkt, book: OrderBook):
        # Call your LLM, news API, etc.
        score = my_model.predict(mkt["question"])
        direction = Side.BUY if score > 0.5 else Side.SELL
        confidence = abs(score - 0.5) * 2
        return direction, confidence, "AI signal"
```

Then register it in `main.py`'s `_run_async()`.

---

## Safety Checklist

Before going live:

- [ ] Run in paper mode for at least 24 hours
- [ ] Verify risk limits are set conservatively
- [ ] Start with small `*_ORDER_SIZE_USDC` values (e.g., 5)
- [ ] Set `DAILY_LOSS_LIMIT_USDC` to an amount you can afford to lose
- [ ] Keep `MAX_TOTAL_EXPOSURE_USDC` below 10% of your wallet balance
- [ ] Have `python main.py cancel-all` ready in another terminal
