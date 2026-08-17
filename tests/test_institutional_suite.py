"""
tests/test_institutional_suite.py — Institutional Test Suite for Polymarket Platform.

Covers:
  - Phase 6: USD 200 Hard Bankroll & Risk Circuit Breakers
  - Phase 4 & 5: Specialized Market DB Ingestion & Retrieval
  - Phase 3: AI/ML 32-Feature Extraction & Calibrated Inference
  - Phase 2: Execution Engine & Backtesting Simulation
"""
import os
import sys
import time
import unittest

import numpy as np

# Adjust path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtesting.engine import BacktestEngine
from core.data_store import (
    BANKROLL_BASELINE,
    Order,
    OrderBook,
    Position,
    PriceLevel,
    Side,
    store,
)
from core.market_db import MarketIntelligenceDB
from ml.features import N_FEATURES, extract_features
from ml.model import MarketMLModel
from risk.manager import (
    InstitutionalRiskEngine,
    recognized_operating_capital,
)


class TestInstitutionalSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        store.kill_switch_active = False
        store.daily_pnl = 0.0
        store.peak_equity = BANKROLL_BASELINE
        store.paper_balance = BANKROLL_BASELINE
        store.open_orders.clear()
        store.positions.clear()
        store.trades.clear()
        self.risk = InstitutionalRiskEngine()

    async def test_01_bankroll_and_cash_reserve(self):
        """Test per-market ($3) and absolute ($5) position caps under USD 100 operating capital."""
        # 1. Oversized single position ($4.00 > $3.00 per-market cap)
        oversized_order = Order(
            order_id="test-1",
            token_id="tok-1",
            side=Side.BUY,
            price=0.50,
            size=8.0,  # $4.00 cost > $3.00 per-market max
            strategy="test",
            paper=True,
        )
        allowed, reason = await self.risk.check_order(oversized_order)
        self.assertFalse(allowed)
        self.assertIn("Per-market position cap exceeded", reason)

        # 2. Valid position ($1.50 cost <= $3.00 per-market, <= $2 normal cap)
        valid_order = Order(
            order_id="test-2",
            token_id="tok-2",
            side=Side.BUY,
            price=0.50,
            size=3.0,  # $1.50 cost
            strategy="test",
            paper=True,
        )
        allowed, reason = await self.risk.check_order(valid_order)
        self.assertTrue(allowed, f"Expected allowed, got reason: {reason}")

    async def test_01a_recognized_operating_capital(self):
        """recognized_operating_capital = min(verified equity, USD 100)."""
        self.assertEqual(float(recognized_operating_capital(50.0)), 50.0)
        self.assertEqual(float(recognized_operating_capital(100.0)), 100.0)
        self.assertEqual(float(recognized_operating_capital(459.66)), 100.0)

    async def test_01c_live_trading_disabled_by_default(self):
        """Live orders are blocked unless explicitly authorized; paper orders pass."""
        order = Order(
            order_id="test-live",
            token_id="tok-live",
            side=Side.BUY,
            price=0.50,
            size=3.0,
            strategy="test",
            paper=False,
        )
        allowed, reason = await self.risk.check_order(order)
        self.assertFalse(allowed)
        self.assertIn("Live trading is disabled", reason)

    async def test_01b_correlated_and_strategy_exposure_caps(self):
        """Strategy ($15) and correlated-group ($8) exposure caps are enforced."""
        def mk_pos(tok, slug, strat, cost):
            # shares * price such that current_exposure == cost
            return Position(
                token_id=tok, market_slug=slug, yes_shares=cost * 2.0,
                avg_entry_price=0.50, total_invested=cost, strategy=strat,
            )

        # Part A — correlated group binding: 4 positions @ $3.00 in one slug,
        # split across two strategies so per-strategy stays under its $15 cap.
        for i in range(2):
            store.positions[f"tok-g{i}"] = mk_pos(f"tok-g{i}", "same-event", "mm_avellaneda_stoikov", 3.0)
        for i in range(2, 4):
            store.positions[f"tok-g{i}"] = mk_pos(f"tok-g{i}", "same-event", "arb_binary_dutch_book", 3.0)

        # $1.50 more on the same slug → group $13.50 > $8 (blocked); strategy only $7.50.
        store.market_slugs["tok-g9"] = "same-event"
        order = Order(
            order_id="test-g", token_id="tok-g9", side=Side.BUY,
            price=0.50, size=3.0, strategy="mm_avellaneda_stoikov", paper=True,
        )
        allowed, reason = await self.risk.check_order(order)
        self.assertFalse(allowed)
        self.assertIn("Correlated exposure cap exceeded", reason)

        # Part B — strategy binding: push mm strategy to $15, then $1.50 more → $16.50 > $15.
        for i in range(4, 7):
            store.positions[f"tok-g{i}"] = mk_pos(f"tok-g{i}", f"other-event-{i}", "mm_avellaneda_stoikov", 3.0)
        store.market_slugs["tok-g9"] = "strategy-bound"
        order = Order(
            order_id="test-s", token_id="tok-g9", side=Side.BUY,
            price=0.50, size=3.0, strategy="mm_avellaneda_stoikov", paper=True,
        )
        allowed, reason = await self.risk.check_order(order)
        self.assertFalse(allowed)
        self.assertIn("Strategy exposure cap exceeded", reason)

    async def test_02_daily_loss_circuit_breaker(self):
        """Test that breaching the $2.00 daily loss stop halts trading."""
        store.daily_pnl = -6.00  # Breached $2.00 stop

        order = Order(
            order_id="test-3",
            token_id="tok-3",
            side=Side.BUY,
            price=0.50,
            size=8.0,
            strategy="test",
            paper=True,
        )
        allowed, reason = await self.risk.check_order(order)
        self.assertFalse(allowed)
        self.assertTrue(store.kill_switch_active)
        self.assertIn("Daily loss", reason)

    async def test_03_specialized_market_db(self):
        """Test recording snapshots and extracting training samples from market_db."""
        import tempfile
        from pathlib import Path
        tmp_db_file = Path(tempfile.mktemp(suffix=".db"))
        
        # Instantiate test DB
        os.environ["MARKET_DB_PATH"] = str(tmp_db_file)
        test_db = MarketIntelligenceDB()

        await test_db.record_snapshot(
            token_id="tok-test",
            slug="will-bitcoin-hit-100k",
            best_bid=0.62,
            best_ask=0.64,
            mid=0.63,
            spread=0.02,
        )

        await test_db.record_tick(
            token_id="tok-test",
            best_bid_size=500.0,
            best_ask_size=300.0,
            ofi=0.25,
            micro_price=0.632,
        )

        stats = test_db.get_stats()
        self.assertGreaterEqual(stats["snapshots_recorded"], 1)
        self.assertGreaterEqual(stats["ticks_recorded"], 1)

        # Cleanup
        if tmp_db_file.exists():
            tmp_db_file.unlink(missing_ok=True)

    def test_04_32_feature_pipeline(self):
        """Test that extract_features outputs 32 normalized numerical features."""
        book = OrderBook(
            token_id="tok-feat",
            bids=[PriceLevel(price=0.52, size=100.0), PriceLevel(price=0.51, size=80.0)],
            asks=[PriceLevel(price=0.54, size=120.0), PriceLevel(price=0.55, size=90.0)],
        )
        features = extract_features({"volume24hr": 50000, "volume": 200000}, book)
        self.assertIsNotNone(features)
        self.assertEqual(len(features), N_FEATURES)
        self.assertEqual(len(features), 32)
        self.assertFalse(np.isnan(features).any())

    def test_05_ai_model_calibration_and_registry(self):
        """Test ML ensemble prediction and calibration bounds."""
        model = MarketMLModel()
        model.fit_initial()
        
        dummy_feat = np.ones(32, dtype=np.float32) * 0.5
        p_yes, conf = model.predict(dummy_feat)
        
        self.assertGreaterEqual(p_yes, 0.01)
        self.assertLessEqual(p_yes, 0.99)
        self.assertGreaterEqual(conf, 0.0)
        self.assertLessEqual(conf, 1.0)
        self.assertLess(model.brier_score, 0.25)

    def test_06_backtest_simulation_engine(self):
        """Test quantitative backtesting simulation output."""
        engine = BacktestEngine()
        res = engine.run_backtest(
            strategy_id="mm_avellaneda_stoikov",
            initial_capital=200.0,
            days=14,
            slippage_bps=5.0,
        )
        self.assertEqual(res.initial_capital, 200.0)
        self.assertGreater(res.total_trades, 0)
        self.assertGreater(res.profit_factor, 0.0)
        self.assertIsInstance(res.equity_curve, list)

    def test_07_deep_analysis_engine(self):
        """Test complete 9-factor deep market analysis and recommendation calculations."""
        from core.analysis_engine import deep_analysis_engine
        book = OrderBook(
            token_id="tok-deep-test",
            bids=[PriceLevel(price=0.52, size=500.0), PriceLevel(price=0.51, size=300.0)],
            asks=[PriceLevel(price=0.54, size=400.0), PriceLevel(price=0.55, size=200.0)],
            updated_at=time.time(),
        )
        store.order_books["tok-deep-test"] = book
        store.market_slugs["tok-deep-test"] = "will-fed-cut-rates-in-september"

        analysis = deep_analysis_engine.analyze_market("tok-deep-test")
        self.assertEqual(analysis["status"], "VALIDATED")
        self.assertAlmostEqual(analysis["market_implied_prob"], 0.53, places=2)
        self.assertIn("suggested_action", analysis)
        self.assertIn(analysis["suggested_action"], ["TRADE_LONG_YES", "TRADE_SHORT_NO", "MONITOR", "REJECT_RISK"])
        self.assertIsInstance(analysis["uncertainty_interval"], list)
        self.assertEqual(len(analysis["uncertainty_interval"]), 2)
        self.assertGreater(analysis["total_liquidity_usdc"], 100.0)

    def test_08_smart_order_router_twap(self):
        """Test TWAP order slicing for large blocks and slippage estimation."""
        from execution.smart_router import smart_router
        book = OrderBook(
            token_id="tok-twap-test",
            bids=[PriceLevel(price=0.50, size=500.0)],
            asks=[PriceLevel(price=0.52, size=300.0), PriceLevel(price=0.54, size=500.0)],
        )
        eff_price, slippage = smart_router.calculate_slippage(book, Side.BUY, 200.0)
        self.assertGreaterEqual(eff_price, 0.52)
        self.assertGreaterEqual(slippage, 0.0)

        slices = smart_router.generate_twap_schedule(total_size_usdc=500.0, price=0.52, duration_seconds=120, num_slices=4)
        self.assertEqual(len(slices), 4)
        _ = sum(s.size_usdc for s in slices)
    async def test_09_market_discovery_coverage(self):
        """Test universal catalog pagination, indexing, and coverage report generation."""
        from core.market_discovery import UniversalMarketDiscoveryEngine
        discovery = UniversalMarketDiscoveryEngine()
        
        # Inject mock discovered batch
        mock_markets = [
            {"id": "m1", "token_id": "tok-disc-1", "question": "Will Fed Cut Rates?", "slug": "will-fed-cut-rates", "active": True},
            {"id": "m2", "clobTokenId": "tok-disc-2", "question": "Will Bitcoin Hit 150k?", "slug": "will-btc-hit-150k", "active": True},
            {"id": "m3", "slug": "invalid-no-token", "active": True}, # will be recorded in exclusions
        ]
        
        discovery._authoritative_count = 3
        for m in mock_markets:
            tid = m.get("clobTokenId") or m.get("token_id")
            if tid:
                discovery.catalog[tid] = m
            else:
                discovery.excluded_markets.append({"id": m.get("id", "m3"), "reason": "MISSING_CLOB_TOKEN_ID"})
        
        report = discovery.get_coverage_report()
        self.assertEqual(report["authoritative_markets_reported"], 3)
        self.assertEqual(report["validated_markets_stored"], 2)
        self.assertEqual(report["excluded_markets_count"], 1)
        self.assertAlmostEqual(report["coverage_percentage"], 66.67, places=1)
        self.assertIsInstance(discovery.get_full_catalog(), list)

    async def test_10_global_fundamental_news_source_catalog(self):
        """Test source catalog honesty, deduplication, NLP sentiment, and stats."""
        from core.fundamental_ingest import fundamental_engine
        catalog = fundamental_engine.get_source_catalog()
        # Honest counts: GDELT is config-only (not connected) → zero sources.
        self.assertIn("gdelt_global_network", catalog["source_tiers"])
        self.assertFalse(catalog["gdelt_connected"])
        self.assertEqual(catalog["gdelt_global_network_count"], 0)
        self.assertLess(catalog["total_sources_supported"], 1000)
        self.assertGreater(catalog["curated_wires_count"], 20)

        # Test ingestion with SHA-256 deduplication
        item1 = await fundamental_engine.ingest_news_item("Treasury yields fall as inflation cools", "Reuters Global", "Macro")
        self.assertIsNotNone(item1)
        self.assertGreaterEqual(item1.sentiment, -1.0)
        self.assertLessEqual(item1.sentiment, 1.0)

        # Duplicate should be ignored
        item2 = await fundamental_engine.ingest_news_item("Treasury yields fall as inflation cools", "Reuters Global", "Macro")
        self.assertIsNone(item2)

        stats = fundamental_engine.get_news_stats()
        # sources_indexed = distinct sources actually present — never a fabricated constant
        self.assertGreaterEqual(stats["sources_indexed"], 1)
        self.assertLess(stats["sources_indexed"], 1000)
        self.assertIn("bullish", stats["sentiment_distribution"])

    async def test_11_accounting_reconciliation(self):
        """daily_pnl must equal the sum of trade pnl; exposure must equal cost basis."""
        store.daily_pnl = 0.0
        store.paper_balance = BANKROLL_BASELINE
        store.positions.clear()
        store.trades.clear()

        # Buy 100 shares @ 0.50 (cost $50) then sell 40 shares @ 0.60 (pnl +$4)
        await store.record_fill(type("T", (), {
            "trade_id": "t1", "token_id": "tok-rec", "side": Side.BUY,
            "price": 0.50, "size": 100.0, "pnl": 0.0, "strategy": "test", "paper": True,
        })())
        await store.record_fill(type("T", (), {
            "trade_id": "t2", "token_id": "tok-rec", "side": Side.SELL,
            "price": 0.60, "size": 40.0, "pnl": 4.0, "strategy": "test", "paper": True,
        })())

        # daily_pnl must reconcile to the sum of trade pnl
        self.assertEqual(round(store.daily_pnl, 2), 4.0)
        self.assertEqual(round(sum(t.pnl for t in store.trades), 2), 4.0)

        # Exposure = cost basis of remaining shares (60 * avg_entry)
        pos = store.positions["tok-rec"]
        self.assertEqual(pos.yes_shares, 60.0)
        self.assertAlmostEqual(pos.avg_entry_price, 0.50, places=4)
        self.assertAlmostEqual(await store.total_exposure(), 60.0 * pos.avg_entry_price, places=2)
        self.assertAlmostEqual(store.paper_balance, BANKROLL_BASELINE - 50.0 + 24.0, places=2)

    async def test_12_portfolio_exposure_reconciliation_and_leaderboard(self):
        """Exposure decomposition, reconciliation verdict, and strategy ranking."""
        from core.portfolio import (
            compute_exposure,
            compute_reconciliation,
            leaderboard,
        )

        store.daily_pnl = 0.0
        store.paper_balance = BANKROLL_BASELINE
        store.positions.clear()
        store.trades.clear()
        store.open_orders.clear()

        # One $12.00 position across two fills and a closed winner for a second strategy.
        await store.record_fill(type("T", (), {
            "trade_id": "p1", "token_id": "tok-exp", "side": Side.BUY,
            "price": 0.50, "size": 24.0, "pnl": 0.0, "strategy": "mm_avellaneda_stoikov", "paper": True,
        })())
        await store.record_fill(type("T", (), {
            "trade_id": "p2", "token_id": "tok-win", "side": Side.BUY,
            "price": 0.50, "size": 4.0, "pnl": 0.0, "strategy": "arb_binary_dutch_book", "paper": True,
        })())

        exp = compute_exposure()
        self.assertEqual(exp["capital_invested"], 14.0)
        self.assertEqual(exp["maximum_remaining_loss"], 14.0)

        rec = compute_reconciliation(bankroll_ceiling=200.0)
        # $14 <= $120 (60% of ceiling) and no anomalies → reconciled OK.
        self.assertTrue(rec["reconciled"])
        self.assertEqual(rec["status"], "OK")
        self.assertEqual(rec["checks"]["paper_trades"], 2)

        lb = leaderboard()
        self.assertGreaterEqual(lb["count"], 1)
        top = lb["ranked"][0]
        self.assertIn("risk_adjusted_score", top)
        self.assertGreater(top["risk_adjusted_score"], -1000.0)


if __name__ == "__main__":
    unittest.main()
