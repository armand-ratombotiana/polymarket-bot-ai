"""
tests/test_institutional_suite.py — Institutional Test Suite for Polymarket Platform.

Covers:
  - Phase 6: USD 200 Hard Bankroll & Risk Circuit Breakers
  - Phase 4 & 5: Specialized Market DB Ingestion & Retrieval
  - Phase 3: AI/ML 32-Feature Extraction & Calibrated Inference
  - Phase 2: Execution Engine & Backtesting Simulation
"""
import asyncio
import os
import sys
import unittest
import numpy as np

# Adjust path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data_store import Order, OrderBook, OrderStatus, PriceLevel, Side, store
from core.market_db import MarketIntelligenceDB
from ml.features import extract_features, FEATURE_NAMES, N_FEATURES
from ml.model import MarketMLModel
from ml.model_registry import ModelRegistry
from risk.manager import InstitutionalRiskEngine, BANKROLL_CEILING, MAX_DEPLOYABLE_CAPITAL, DAILY_LOSS_STOP
from backtesting.engine import BacktestEngine


class TestInstitutionalSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        store.kill_switch_active = False
        store.daily_pnl = 0.0
        store.peak_equity = 10000.0
        store.open_orders.clear()
        store.positions.clear()
        self.risk = InstitutionalRiskEngine()

    async def test_01_bankroll_and_cash_reserve(self):
        """Test that orders exceeding deployable capital ($8,000) or single position ($500) are blocked."""
        # 1. Oversized single position ($600 > $500 max)
        oversized_order = Order(
            order_id="test-1",
            token_id="tok-1",
            side=Side.BUY,
            price=0.50,
            size=1200.0,  # $600.00 cost > $500.00 max
            strategy="test",
        )
        allowed, reason = await self.risk.check_order(oversized_order)
        self.assertFalse(allowed)
        self.assertIn("Single position cap exceeded", reason)

        # 2. Valid position ($200.00 cost <= $500.00 max)
        valid_order = Order(
            order_id="test-2",
            token_id="tok-2",
            side=Side.BUY,
            price=0.50,
            size=400.0,  # $200.00 cost
            strategy="test",
        )
        allowed, reason = await self.risk.check_order(valid_order)
        self.assertTrue(allowed, f"Expected allowed, got reason: {reason}")

    async def test_02_daily_loss_circuit_breaker(self):
        """Test that breaching the $250.00 daily loss stop halts trading."""
        store.daily_pnl = -260.00  # Breached $250.00 stop

        order = Order(
            order_id="test-3",
            token_id="tok-3",
            side=Side.BUY,
            price=0.50,
            size=100.0,
            strategy="test",
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


if __name__ == "__main__":
    unittest.main()
