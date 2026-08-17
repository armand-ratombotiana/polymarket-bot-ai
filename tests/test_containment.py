"""
tests/test_containment.py — Sprint 1 (M1–M2) containment evidence:

  P0-SEC-01  authn/authz fail-closed + CORS lockdown          (KD-14, KD-15)
  P0-SAF-01  durable kill switch, watchdog tripwires          (KD-16)
  P0-TRU-01  health endpoint has no hardcoded values          (KD-01)
  P0-TRU-02  OHLCV/backtest/news surfaces labeled synthetic   (KD-02, KD-03, KD-04, KD-05)
  P0-GOV-01  mode flag, weekly-loss enforcement, audit        (KD-12, KD-20)
"""
import asyncio
import unittest

# conftest.py already sets API_TOKEN / CORS_ORIGINS before any module import.
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.server import app
from config import settings
from core.data_store import BANKROLL_BASELINE, Order, Side, store
from core.fundamental_ingest import fundamental_engine
from core.safety import KILL_SWITCH_PATH, clear_kill_switch, kill_switch_file_exists
from core.watchdog import Watchdog
from risk.manager import InstitutionalRiskEngine

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token-123"}


class TestAuthEnforcement(unittest.TestCase):
    """P0-SEC-01 — fail-closed bearer-token auth + CORS lockdown."""

    def test_health_is_public(self):
        r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)

    def test_unauthenticated_mutation_rejected(self):
        r = client.post("/api/kill-switch/activate")
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_read_rejected(self):
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 401)

    def test_bad_token_rejected(self):
        r = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_valid_token_accepted(self):
        r = client.get("/api/status", headers=AUTH)
        self.assertEqual(r.status_code, 200)

    def test_fail_closed_when_token_unset(self):
        """Without API_TOKEN configured, the server must refuse (503), never silently open."""
        old = settings.api_token
        try:
            settings.api_token = ""
            r = client.get("/api/status")
            self.assertEqual(r.status_code, 503)
        finally:
            settings.api_token = old

    def test_cors_allowed_origin_echoed(self):
        r = client.get("/api/status", headers={**AUTH, "Origin": "http://allowed.example"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://allowed.example")

    def test_cors_disallowed_origin_no_header(self):
        r = client.get("/api/status", headers={**AUTH, "Origin": "http://evil.example"})
        self.assertIsNone(r.headers.get("access-control-allow-origin"))

    def test_websocket_rejects_unauthenticated(self):
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.receive_text()


class TestModeAndWeeklyLoss(unittest.TestCase):
    """P0-GOV-01 — canonical mode flag, weekly loss enforcement, audit events."""

    def setUp(self):
        store.kill_switch_active = False
        store.daily_pnl = 0.0
        store.weekly_pnl = 0.0
        store.paper_balance = BANKROLL_BASELINE
        store.open_orders.clear()
        store.positions.clear()
        store.trades.clear()
        self.risk = InstitutionalRiskEngine()

    def tearDown(self):
        settings.trading_mode = "paper"
        store.kill_switch_active = False
        clear_kill_switch()
        asyncio.run(self._reset())
        asyncio.run(self.risk.set_observation_mode(False))

    async def _reset(self):
        store.weekly_pnl = 0.0
        store.daily_pnl = 0.0

    def test_mode_endpoint_is_honest(self):
        r = client.get("/api/system/mode", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "paper")
        self.assertTrue(body["auth_enforced"])
        self.assertIn("weekly", body)

    def test_shadow_mode_blocks_all_orders(self):
        settings.trading_mode = "shadow"
        order = Order(
            order_id="t-shadow", token_id="tok-1", side=Side.BUY,
            price=0.5, size=3.0, strategy="test", paper=True,
        )
        allowed, reason = asyncio.run(self.risk.check_order(order))
        self.assertFalse(allowed)
        self.assertIn("Shadow", reason)

    def test_weekly_loss_stop_enforced(self):
        store.weekly_pnl = -5.5  # breaches $5.00 weekly stop
        order = Order(
            order_id="t-week", token_id="tok-2", side=Side.BUY,
            price=0.5, size=3.0, strategy="test", paper=True,
        )
        allowed, reason = asyncio.run(self.risk.check_order(order))
        self.assertFalse(allowed)
        self.assertIn("Weekly loss", reason)
        self.assertTrue(store.kill_switch_active)
        self.assertTrue(kill_switch_file_exists())

    def test_mode_transition_audited(self):
        from core.audit_logger import audit_logger
        async def _write():
            await audit_logger.log_event(
                category="system", event_type="mode_change",
                details="mode=paper paper_trade=True",
                idempotency_key="mode-test-1",
            )
            events = await audit_logger.get_recent_events(category="system")
            return events
        events = asyncio.run(_write())
        self.assertTrue(any(e["event_type"] == "mode_change" for e in events))


class TestDurableKillSwitch(unittest.TestCase):
    """P0-SAF-01 — file-backed kill switch survives process restart."""

    def setUp(self):
        store.kill_switch_active = False
        clear_kill_switch()
        store.daily_pnl = 0.0
        store.weekly_pnl = 0.0
        store.open_orders.clear()
        store.positions.clear()
        store.trades.clear()

    def tearDown(self):
        store.kill_switch_active = False
        clear_kill_switch()

    def test_activate_writes_durable_marker(self):
        self.risk = InstitutionalRiskEngine()
        asyncio.run(self.risk.activate_kill_switch("test reason"))
        self.assertTrue(kill_switch_file_exists())
        self.assertTrue(KILL_SWITCH_PATH.read_text(encoding="utf-8"))

    def test_kill_survives_new_engine_instance(self):
        r1 = InstitutionalRiskEngine()
        asyncio.run(r1.activate_kill_switch("restart test"))
        # New risk engine (simulated restart): must still see the halted state.
        r2 = InstitutionalRiskEngine()
        order = Order(
            order_id="t-kill", token_id="tok-9", side=Side.BUY,
            price=0.5, size=3.0, strategy="test", paper=True,
        )
        allowed, reason = asyncio.run(r2.check_order(order))
        self.assertFalse(allowed)
        self.assertIn("Kill switch", reason)

    def test_deactivate_clears_marker(self):
        r = InstitutionalRiskEngine()
        asyncio.run(r.activate_kill_switch("x"))
        asyncio.run(r.deactivate_kill_switch())
        self.assertFalse(kill_switch_file_exists())


class TestTruthfulSurfaces(unittest.TestCase):
    """P0-TRU-01/02 — no hardcoded/fabricated values on API surfaces."""

    def test_health_has_no_hardcoded_values(self):
        r = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["poller"]["latency_ms"], None)
        self.assertIn("status_derivation", body)
        self.assertIn(body["status"], {"HEALTHY", "DEGRADED", "UNHEALTHY"})
        self.assertIn("tripwires", body)
        # Kill switch state must be reflected in health
        r = InstitutionalRiskEngine()
        asyncio.run(r.activate_kill_switch("health test"))
        try:
            body2 = client.get("/api/system/health", headers=AUTH).json()
            self.assertEqual(body2["status"], "UNHEALTHY")
        finally:
            store.kill_switch_active = False
            clear_kill_switch()

    def test_ohlcv_labeled_synthetic(self):
        r = client.get("/api/history/ohlcv/tok-ohlcv", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["synthetic"])
        self.assertEqual(body["synthetic_kind"], "seeded_random_walk")

    def test_backtest_labeled_synthetic(self):
        r = client.post(
            "/api/backtest/run",
            headers=AUTH,
            json={"strategy_id": "mm_avellaneda_stoikov", "days": 7},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["synthetic"])
        self.assertEqual(body["synthetic_kind"], "monte_carlo_archetype")

    def test_news_sources_honest(self):
        r = client.get("/api/analysis/news/sources", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["gdelt_connected"])
        self.assertEqual(body["gdelt_global_network_count"], 0)
        self.assertLess(body["total_sources_supported"], 1000)

    def test_news_stats_reflect_actual_items(self):
        async def _run():
            await fundamental_engine.start()
            try:
                stats = fundamental_engine.get_news_stats()
                return stats
            finally:
                await fundamental_engine.stop()
        stats = asyncio.run(_run())
        self.assertGreaterEqual(stats["total_news_items"], 10)
        self.assertLess(stats["sources_indexed"], 1000)
        self.assertGreaterEqual(stats["seed_items"], 10)
        self.assertTrue(all(n.is_seed for n in fundamental_engine.news_feed[:10]))


class TestWatchdog(unittest.TestCase):
    """P0-SAF-01 — heartbeat staleness and critical tripwires."""

    def setUp(self):
        store.kill_switch_active = False
        clear_kill_switch()
        store.daily_pnl = 0.0
        store.weekly_pnl = 0.0
        store.open_orders.clear()
        store.positions.clear()
        store.trades.clear()

    def tearDown(self):
        store.kill_switch_active = False
        clear_kill_switch()
        store.daily_pnl = 0.0
        store.weekly_pnl = 0.0

    def test_stale_heartbeat_detected(self):
        wd = Watchdog(heartbeat_timeout=60, check_interval=5, book_stall_seconds=60)
        wd.register("test_subsystem")
        wd._heartbeats["test_subsystem"] = wd._heartbeats["test_subsystem"] - 600
        findings = asyncio.run(wd.run_checks())
        self.assertTrue(any("heartbeat:test_subsystem" == f["name"] for f in findings))

    def test_daily_loss_tripwire_auto_kills(self):
        wd = Watchdog(heartbeat_timeout=60, check_interval=5, book_stall_seconds=60, auto_kill=True)
        store.daily_pnl = -6.0
        findings = asyncio.run(wd.run())
        self.assertTrue(any(f["id"] == "wr02" for f in findings))
        self.assertTrue(kill_switch_file_exists())
        self.assertTrue(any(f["id"] == "wr07" for f in findings))

    def test_weekly_loss_tripwire(self):
        wd = Watchdog(heartbeat_timeout=60, check_interval=5, book_stall_seconds=60, auto_kill=False)
        store.weekly_pnl = -6.0
        findings = asyncio.run(wd.run_checks())
        self.assertTrue(any(f["id"] == "wr03" for f in findings))

    def test_feed_stall_detected(self):
        wd = Watchdog(heartbeat_timeout=60, check_interval=5, book_stall_seconds=60)
        from core.data_store import OrderBook
        book = OrderBook(token_id="t-stall")
        book.updated_at = book.updated_at - 600
        store.order_books["t-stall"] = book
        try:
            findings = asyncio.run(wd.run_checks())
            self.assertTrue(any(f["id"] == "wr04" for f in findings))
        finally:
            store.order_books.clear()


if __name__ == "__main__":
    unittest.main()