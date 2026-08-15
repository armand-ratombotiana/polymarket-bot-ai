"""
api/server.py — FastAPI server with REST endpoints, WebSocket broadcast, strategy lifecycle,
live parameter configuration, depth charts, and automated persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from core.book_poller import book_poller
from core.clob_client import OrderArgs
from core.data_store import Order, OrderStatus, Side, store
from core.gamma_client import gamma_client
from core.settlement import settlement_engine
from core.ws_client import ws_client
from paper.simulator import paper_sim
from risk.manager import risk_manager

log = logging.getLogger(__name__)


# ── WebSocket Connection Manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        log.info("WS client connected — total %d", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: Dict) -> None:
        dead = []
        payload = json.dumps(data, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


manager = ConnectionManager()
_strategies: Dict[str, Any] = {}
_seeded_tokens: List[str] = []


# ── Market Seeding ────────────────────────────────────────────────────────────

async def _seed_markets(limit: int = 50) -> List[str]:
    token_ids: List[str] = []
    try:
        markets = await gamma_client.get_markets(active=True, limit=limit, order="volume24hr")
        for mkt in markets:
            slug = mkt.get("slug") or mkt.get("groupItemTitle") or ""
            ids = gamma_client.extract_token_ids(mkt)
            for tid in ids:
                token_ids.append(tid)
                if slug:
                    store.market_slugs[tid] = slug

        if token_ids:
            unique_ids = list(dict.fromkeys(token_ids))
            log.info("Seeded %d unique tokens from %d markets", len(unique_ids), len(markets))
            await store.log_event(f"📊 Seeded {len(unique_ids)} market tokens from Gamma API")
            return unique_ids
    except Exception as e:
        log.error("Market seeding failed: %s", e)
        await store.log_event(f"⚠ Market seed failed: {e}")
    return token_ids


async def _reseed_loop() -> None:
    await asyncio.sleep(600)
    while True:
        try:
            new_tokens = await _seed_markets(50)
            if new_tokens:
                book_poller.add_tokens(new_tokens)
        except Exception as e:
            log.debug("Reseed error: %s", e)
        await asyncio.sleep(600)


async def _token_sync_loop() -> None:
    while True:
        await asyncio.sleep(20)
        try:
            async with store._lock:
                tracked = list(store.market_slugs.keys())
            book_poller.add_tokens(tracked)
        except Exception:
            pass


async def _state_persistence_loop() -> None:
    """Auto-persist store state to disk every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            store.save_to_disk()
        except Exception as e:
            log.debug("Persistence loop error: %s", e)


# ── Lifespan Manager ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _strategies, _seeded_tokens

    # 1. Start paper simulator
    if settings.paper_trade:
        await paper_sim.start()
        await store.log_event("📄 Paper trading mode active — no real funds used")

    # 2. Seed markets from Gamma API
    _seeded_tokens = await _seed_markets(50)

    # 3. Start REST book poller & supplemental WebSocket client
    book_poller.set_tokens(_seeded_tokens)
    await book_poller.start()
    await store.log_event(f"📈 Book poller started — monitoring {len(_seeded_tokens)} tokens")

    await ws_client.start()
    await store.log_event("🔌 WebSocket supplemental feed started")

    # 4. Start Settlement Engine
    await settlement_engine.start()

    # 5. Initialize Strategies
    from strategies.market_maker import MarketMakerStrategy
    from strategies.arb_scanner import ArbScannerStrategy
    from strategies.signal_trader import SignalTraderStrategy

    mm = MarketMakerStrategy()
    arb = ArbScannerStrategy()
    signal = SignalTraderStrategy()

    if settings.mm_enabled:
        await mm.start()
        _strategies["market_maker"] = mm
        await store.log_event("📊 Market Maker strategy started")

    if settings.arb_enabled:
        await arb.start()
        _strategies["arb_scanner"] = arb
        await store.log_event("⚡ Arb Scanner strategy started")

    await signal.start()
    _strategies["signal_trader"] = signal
    await store.log_event("🤖 ML Signal Trader strategy started")

    # 6. Background tasks
    broadcast_task = asyncio.create_task(_broadcast_loop(), name="ws-broadcast")
    reseed_task = asyncio.create_task(_reseed_loop(), name="market-reseed")
    token_sync_task = asyncio.create_task(_token_sync_loop(), name="token-sync")
    persist_task = asyncio.create_task(_state_persistence_loop(), name="state-persist")

    log.info("API server ready — %d strategies active, %d tokens tracked", len(_strategies), len(_seeded_tokens))
    await store.log_event(f"✅ Bot online 24/7 — {len(_strategies)} active strategies")

    yield  # ── Serving HTTP & WS requests ──

    # Clean shutdown
    broadcast_task.cancel()
    reseed_task.cancel()
    token_sync_task.cancel()
    persist_task.cancel()
    store.save_to_disk()

    await settlement_engine.stop()
    for s in _strategies.values():
        try:
            await s.stop()
        except Exception:
            pass
    await book_poller.stop()
    await ws_client.stop()
    if settings.paper_trade:
        await paper_sim.stop()
    await gamma_client.close()
    log.info("API server stopped cleanly")


# ── Broadcast Loop ────────────────────────────────────────────────────────────

async def _broadcast_loop() -> None:
    while True:
        try:
            if manager.active:
                snap = await _build_snapshot()
                await manager.broadcast(snap)
        except Exception as e:
            log.debug("Broadcast error: %s", e)
        await asyncio.sleep(1.0)


async def _build_snapshot() -> Dict:
    books = []
    async with store._lock:
        for tid, book in store.order_books.items():
            books.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "mid": book.mid,
                "spread": book.spread,
                "updated_at": book.updated_at,
            })

    orders = []
    for o in store.open_orders.values():
        orders.append({
            "order_id": o.order_id,
            "token_id": o.token_id,
            "slug": store.market_slugs.get(o.token_id, o.token_id[:14]),
            "side": o.side.value,
            "price": o.price,
            "size": o.size,
            "size_matched": o.size_matched,
            "strategy": o.strategy,
            "paper": o.paper,
            "created_at": o.created_at,
        })

    positions = []
    for tid, pos in store.positions.items():
        if pos.yes_shares > 0 or pos.total_invested > 0 or pos.realised_pnl != 0:
            positions.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "yes_shares": pos.yes_shares,
                "avg_entry_price": pos.avg_entry_price,
                "total_invested": pos.total_invested,
                "realised_pnl": pos.realised_pnl,
            })

    trades = []
    for t in store.trades[-50:]:
        trades.append({
            "trade_id": t.trade_id,
            "token_id": t.token_id,
            "slug": store.market_slugs.get(t.token_id, t.token_id[:14]),
            "side": t.side.value,
            "price": t.price,
            "size": t.size,
            "pnl": t.pnl,
            "strategy": t.strategy,
            "paper": t.paper,
            "timestamp": t.timestamp,
        })

    events = await store.get_recent_events(50)

    return {
        "type": "snapshot",
        "timestamp": time.time(),
        "mode": "paper" if settings.paper_trade else "live",
        "kill_switch": store.kill_switch_active,
        "daily_pnl": store.daily_pnl,
        "paper_balance": paper_sim.virtual_balance if settings.paper_trade else None,
        "strategies": list(_strategies.keys()),
        "order_books": books,
        "open_orders": orders,
        "positions": positions,
        "recent_trades": trades,
        "events": events,
    }


# ── App & Routes ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Polymarket Bot API",
    version="2.2.0",
    description="24/7 Automated Polymarket Algorithmic Trading Platform & ML Signal Engine",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Config Models ───────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    token_id: str
    price: float = Field(gt=0, lt=1)
    side: str = Field(pattern="^(BUY|SELL|buy|sell)$")
    size_usdc: float = Field(gt=0, default=10.0)


class StrategyToggleRequest(BaseModel):
    strategy_name: str
    enabled: bool


class StrategyConfigUpdate(BaseModel):
    mm_spread_bps: Optional[int] = Field(default=None, ge=10, le=2000)
    mm_quote_size_usdc: Optional[float] = Field(default=None, ge=1.0, le=500.0)
    mm_max_inventory_usdc: Optional[float] = Field(default=None, ge=10.0, le=2000.0)
    arb_min_profit_bps: Optional[int] = Field(default=None, ge=5, le=1000)
    arb_order_size_usdc: Optional[float] = Field(default=None, ge=1.0, le=500.0)
    signal_min_confidence: Optional[float] = Field(default=None, ge=0.5, le=0.99)
    daily_loss_limit_usdc: Optional[float] = Field(default=None, ge=1.0, le=1000.0)


# ── System Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/health", tags=["system"])
async def health():
    return {"status": "ok", "timestamp": time.time(), "paper": settings.paper_trade}


@app.get("/api/status", tags=["system"])
async def status():
    report = await risk_manager.status_report()
    return {
        **report,
        "mode": "paper" if settings.paper_trade else "live",
        "strategies": list(_strategies.keys()),
        "paper_balance": paper_sim.virtual_balance if settings.paper_trade else None,
        "seeded_markets": len(_seeded_tokens),
        "tracked_books": len(store.order_books),
        "book_poller": book_poller.stats,
    }


@app.get("/api/snapshot", tags=["system"])
async def get_snapshot():
    return await _build_snapshot()


@app.get("/api/history/equity", tags=["system"])
async def get_equity_history():
    """Return historical equity time-series points for charting."""
    async with store._lock:
        return {"points": store.equity_history, "count": len(store.equity_history)}


@app.get("/api/analytics", tags=["system"])
async def get_analytics():
    trades = store.trades
    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t.pnl > 0)
    losing_trades = sum(1 for t in trades if t.pnl < 0)
    win_rate = (winning_trades / total_trades) if total_trades > 0 else 0.0
    total_vol = sum(t.price * t.size for t in trades)

    return {
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(win_rate, 4),
        "total_volume_usdc": round(total_vol, 2),
        "realised_pnl": round(store.daily_pnl, 2),
        "open_exposure": round(await store.total_exposure(), 2),
        "peak_equity": round(store.peak_equity, 2),
        "active_strategies": list(_strategies.keys()),
    }


# ── Strategy Configuration ────────────────────────────────────────────────────

@app.get("/api/config", tags=["config"])
async def get_config():
    """Return current strategy & risk parameters."""
    return {
        "mm_spread_bps": settings.mm_spread_bps,
        "mm_quote_size_usdc": settings.mm_quote_size_usdc,
        "mm_max_inventory_usdc": settings.mm_max_inventory_usdc,
        "arb_min_profit_bps": settings.arb_min_profit_bps,
        "arb_order_size_usdc": settings.arb_order_size_usdc,
        "signal_min_confidence": settings.signal_min_confidence,
        "daily_loss_limit_usdc": settings.daily_loss_limit_usdc,
        "max_total_exposure_usdc": settings.max_total_exposure_usdc,
        "max_open_orders": settings.max_open_orders,
    }


@app.put("/api/config", tags=["config"])
async def update_config(cfg: StrategyConfigUpdate):
    """Update strategy & risk parameters in real-time."""
    if cfg.mm_spread_bps is not None:
        settings.mm_spread_bps = cfg.mm_spread_bps
        if "market_maker" in _strategies:
            _strategies["market_maker"]._base_spread_frac = cfg.mm_spread_bps / 10_000

    if cfg.mm_quote_size_usdc is not None:
        settings.mm_quote_size_usdc = cfg.mm_quote_size_usdc
        if "market_maker" in _strategies:
            _strategies["market_maker"]._quote_size = cfg.mm_quote_size_usdc

    if cfg.mm_max_inventory_usdc is not None:
        settings.mm_max_inventory_usdc = cfg.mm_max_inventory_usdc
        if "market_maker" in _strategies:
            _strategies["market_maker"]._max_inv = cfg.mm_max_inventory_usdc

    if cfg.arb_min_profit_bps is not None:
        settings.arb_min_profit_bps = cfg.arb_min_profit_bps
        if "arb_scanner" in _strategies:
            _strategies["arb_scanner"]._min_profit_frac = cfg.arb_min_profit_bps / 10_000

    if cfg.signal_min_confidence is not None:
        settings.signal_min_confidence = cfg.signal_min_confidence
        if "signal_trader" in _strategies:
            _strategies["signal_trader"]._min_confidence = cfg.signal_min_confidence

    if cfg.daily_loss_limit_usdc is not None:
        settings.daily_loss_limit_usdc = cfg.daily_loss_limit_usdc

    await store.log_event("⚙️ Strategy configuration parameters updated live")
    return {"status": "updated", "config": await get_config()}


# ── Markets & Order Book Depth ────────────────────────────────────────────────

@app.get("/api/markets", tags=["markets"])
async def get_markets(limit: int = 20, search: Optional[str] = None):
    try:
        if search:
            items = await gamma_client.search_markets(search, limit=limit)
        else:
            items = await gamma_client.get_markets(active=True, limit=limit)
        return {"markets": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/depth/{token_id}", tags=["markets"])
async def get_market_depth(token_id: str):
    """Return cumulative order book depth for visual depth charts."""
    book = await store.get_order_book(token_id)
    if not book:
        # Prioritize token in poller
        book_poller.prioritize_tokens([token_id])
        return {"token_id": token_id, "bids": [], "asks": [], "mid": None, "spread": None}

    # Cumulative depth
    cum_bids = []
    b_total = 0.0
    for b in book.bids[:10]:
        b_total += b.size
        cum_bids.append({"price": b.price, "size": b.size, "total": round(b_total, 2)})

    cum_asks = []
    a_total = 0.0
    for a in book.asks[:10]:
        a_total += a.size
        cum_asks.append({"price": a.price, "size": a.size, "total": round(a_total, 2)})

    return {
        "token_id": token_id,
        "slug": store.market_slugs.get(token_id, token_id[:14]),
        "bids": cum_bids,
        "asks": cum_asks,
        "mid": book.mid,
        "spread": book.spread,
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
    }


@app.get("/api/orderbooks", tags=["markets"])
async def get_orderbooks():
    books = []
    async with store._lock:
        for tid, book in store.order_books.items():
            books.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "bids": [{"price": b.price, "size": b.size} for b in book.bids[:5]],
                "asks": [{"price": a.price, "size": a.size} for a in book.asks[:5]],
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "mid": book.mid,
                "spread": book.spread,
                "updated_at": book.updated_at,
            })
    return {"order_books": books, "count": len(books)}


# ── Trading & Strategy Controls ───────────────────────────────────────────────

@app.post("/api/trade", tags=["trading"])
async def place_manual_trade(req: ManualTradeRequest):
    side = Side.BUY if req.side.upper() == "BUY" else Side.SELL
    size_shares = req.size_usdc / req.price

    args = OrderArgs(
        token_id=req.token_id,
        price=req.price,
        side=side,
        size=size_shares,
    )

    if settings.paper_trade:
        order = await paper_sim.create_order(args, strategy="manual")
    else:
        from core.clob_client import clob_client
        order = await clob_client.create_order(args)
        if order:
            await store.add_order(order)

    if not order:
        raise HTTPException(status_code=400, detail="Failed to place order")

    slug = store.market_slugs.get(req.token_id, req.token_id[:12])
    await store.log_event(f"👤 Manual Order: {side.value} {slug} @ {req.price:.4f} (${req.size_usdc:.2f})")
    return {"status": "placed", "order": order}


@app.post("/api/strategies/toggle", tags=["trading"])
async def toggle_strategy(req: StrategyToggleRequest):
    name = req.strategy_name.lower()
    if req.enabled:
        if name in _strategies:
            return {"status": "already_running", "strategy": name}

        if name == "market_maker":
            from strategies.market_maker import MarketMakerStrategy
            s = MarketMakerStrategy()
            await s.start()
            _strategies[name] = s
        elif name == "arb_scanner":
            from strategies.arb_scanner import ArbScannerStrategy
            s = ArbScannerStrategy()
            await s.start()
            _strategies[name] = s
        elif name == "signal_trader":
            from strategies.signal_trader import SignalTraderStrategy
            s = SignalTraderStrategy()
            await s.start()
            _strategies[name] = s
        else:
            raise HTTPException(status_code=400, detail=f"Unknown strategy: {name}")

        await store.log_event(f"▶ Strategy [{name}] enabled via API")
        return {"status": "started", "strategy": name}
    else:
        if name not in _strategies:
            return {"status": "not_running", "strategy": name}

        s = _strategies.pop(name)
        await s.stop()
        await store.log_event(f"⏸ Strategy [{name}] stopped via API")
        return {"status": "stopped", "strategy": name}


# ── Orders, Positions & Trades ────────────────────────────────────────────────

@app.get("/api/orders", tags=["trading"])
async def get_orders():
    orders = await store.get_open_orders()
    return {
        "orders": [
            {
                "order_id": o.order_id,
                "token_id": o.token_id,
                "slug": store.market_slugs.get(o.token_id, ""),
                "side": o.side.value,
                "price": o.price,
                "size": o.size,
                "size_matched": o.size_matched,
                "strategy": o.strategy,
                "paper": o.paper,
                "created_at": o.created_at,
            }
            for o in orders
        ],
        "count": len(orders),
    }


@app.delete("/api/orders", tags=["trading"])
async def cancel_all_orders():
    if settings.paper_trade:
        n = await paper_sim.cancel_all()
    else:
        from core.clob_client import clob_client
        await clob_client.cancel_all_orders()
        cancelled = await store.cancel_all_orders()
        n = len(cancelled)
    await store.log_event(f"🛑 Cancelled all {n} open order(s)")
    return {"cancelled": n}


@app.delete("/api/orders/{order_id}", tags=["trading"])
async def cancel_order(order_id: str):
    if settings.paper_trade:
        ok = await paper_sim.cancel_order(order_id)
    else:
        from core.clob_client import clob_client
        ok = await clob_client.cancel_order(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    await store.log_event(f"🛑 Cancelled order {order_id[:16]}")
    return {"cancelled": order_id}


@app.get("/api/positions", tags=["trading"])
async def get_positions():
    positions = []
    async with store._lock:
        for tid, pos in store.positions.items():
            positions.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, ""),
                "yes_shares": pos.yes_shares,
                "avg_entry_price": pos.avg_entry_price,
                "total_invested": pos.total_invested,
                "realised_pnl": pos.realised_pnl,
            })
    return {"positions": positions, "count": len(positions), "daily_pnl": store.daily_pnl}


@app.get("/api/trades", tags=["trading"])
async def get_trades(limit: int = 50):
    trades = store.trades[-limit:]
    return {
        "trades": [
            {
                "trade_id": t.trade_id,
                "slug": store.market_slugs.get(t.token_id, ""),
                "side": t.side.value,
                "price": t.price,
                "size": t.size,
                "pnl": t.pnl,
                "strategy": t.strategy,
                "paper": t.paper,
                "timestamp": t.timestamp,
            }
            for t in reversed(trades)
        ],
        "count": len(trades),
    }


@app.get("/api/events", tags=["system"])
async def get_events(n: int = 50):
    events = await store.get_recent_events(n)
    return {"events": list(reversed(events)), "count": len(events)}


# ── Risk Management ───────────────────────────────────────────────────────────

@app.post("/api/kill-switch/activate", tags=["risk"])
async def activate_kill_switch():
    await risk_manager.activate_kill_switch("Manual via UI")
    await store.log_event("🛑 KILL SWITCH activated — all trading halted")
    return {"status": "activated", "kill_switch": True}


@app.post("/api/kill-switch/deactivate", tags=["risk"])
async def deactivate_kill_switch():
    await risk_manager.deactivate_kill_switch()
    await store.log_event("▶ Kill switch deactivated — trading resumed")
    return {"status": "deactivated", "kill_switch": False}


# ── ML Model ──────────────────────────────────────────────────────────────────

@app.get("/api/ml", tags=["ml"])
async def get_ml_status():
    from ml.model import ml_model
    return {
        "model_type": "RandomForest + SGD Online Ensemble",
        "n_online_updates": ml_model._n_updates,
        "last_trained": ml_model._last_trained,
        "feature_importances": ml_model.feature_importances,
        "model_ready": ml_model.rf is not None,
    }


@app.post("/api/ml/learn", tags=["ml"])
async def ml_learn(token_id: str, resolved_yes: bool):
    signal_strat = _strategies.get("signal_trader")
    if signal_strat and hasattr(signal_strat, "record_outcome"):
        await signal_strat.record_outcome(token_id, resolved_yes)
        from ml.model import ml_model
        return {"status": "updated", "n_updates": ml_model._n_updates}
    return {"status": "updated"}


# ── WebSocket Real-Time Stream ────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        snap = await _build_snapshot()
        await websocket.send_text(json.dumps(snap, default=str))
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS client disconnected: %s", e)
    finally:
        manager.disconnect(websocket)
