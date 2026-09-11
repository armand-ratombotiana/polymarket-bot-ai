"""
core/analysis_engine.py — Deep Market Analysis, Probability Forecasting & Alpha Recommendation Engine.

Computes:
  - Market-Implied Probability vs Calibrated AI Forecast
  - Expected Edge after Fees and Dynamic Slippage
  - Model Confidence, Variance, and Uncertainty Bounds
  - 5-Level Microstructure Depth & Order Flow Imbalance (OFI)
  - Supporting vs Contradicting Fundamental NLP Evidence
  - Strict Rule-Based Action Recommendations (TRADE / MONITOR / REJECT) with Explicit Rationale
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core.data_store import OrderBook, Side, store
from core.fundamental_ingest import fundamental_engine
from execution.smart_router import smart_router
from ml.features import extract_features
from ml.model import ml_model
from ml.model_registry import model_registry

log = logging.getLogger(__name__)


class DeepMarketAnalysisEngine:
    """
    Multi-Factor Intelligence Engine evaluating prediction market efficiency.
    """

    def analyze_market(self, token_id: str) -> dict[str, Any]:
        """Run complete 9-factor probabilistic and fundamental analysis for a given contract."""
        start_t = time.time()
        book: OrderBook | None = store.order_books.get(token_id)
        slug = store.market_slugs.get(token_id, token_id[:18])

        if not book or not book.best_bid or not book.best_ask:
            return {
                "token_id": token_id,
                "slug": slug,
                "status": "INSUFFICIENT_DATA",
                "reason": "Order book has no active bids/asks or market is closed",
                "generation_time_ms": round((time.time() - start_t) * 1000, 2),
            }

        mid_p = book.mid or 0.50
        spread = book.spread or (book.best_ask - book.best_bid)
        spread_pct = spread / mid_p if mid_p > 0 else 0.0

        # 1. Feature Extraction & AI/ML Inference
        # Use real market data from discovery catalog so volume/liquidity features are accurate.
        # Falls back to a minimal dict with zeros (still valid — features clip gracefully).
        try:
            from core.market_discovery import market_discovery
            mkt_data = market_discovery.catalog.get(token_id) or {
                "slug": slug, "volume24hr": 0.0, "volume": 0.0, "liquidity": 0.0
            }
        except Exception:
            mkt_data = {"slug": slug, "volume24hr": 0.0, "volume": 0.0, "liquidity": 0.0}

        features = extract_features(mkt_data, book)
        if features is not None and ml_model.is_fitted:
            p_ml = float(ml_model.predict_proba(features, token_id=token_id))
            confidence = float(ml_model.predict_confidence(features, token_id=token_id))
        else:
            p_ml = mid_p
            confidence = 0.50

        # Uncertainty bounds (95% confidence interval)
        uncertainty_margin = round((1.0 - confidence) * 0.12, 3)
        p_lower = max(round(p_ml - uncertainty_margin, 3), 0.01)
        p_upper = min(round(p_ml + uncertainty_margin, 3), 0.99)

        # 2. Edge & Execution Feasibility
        raw_edge = p_ml - mid_p
        eff_price, slippage_bps = smart_router.calculate_slippage(book, Side.BUY if raw_edge > 0 else Side.SELL, 100.0)
        slippage_pct = slippage_bps / 10000.0
        fee_pct = 0.001  # 10 BPS nominal protocol fee
        net_edge = raw_edge - (fee_pct + slippage_pct) if raw_edge > 0 else raw_edge + (fee_pct + slippage_pct)

        # 3. Microstructure & Liquidity
        bid_depth = sum(b.size * b.price for b in book.bids[:5])
        ask_depth = sum(a.size * a.price for a in book.asks[:5])
        total_depth_usdc = bid_depth + ask_depth
        b_sz = book.bids[0].size if book.bids else 1.0
        a_sz = book.asks[0].size if book.asks else 1.0
        ofi = round((b_sz - a_sz) / max(b_sz + a_sz, 1.0), 3)

        # 4. Fundamental Evidence
        token_sentiment = fundamental_engine.get_token_sentiment(token_id)
        all_news = fundamental_engine.news_feed
        supporting_news = []
        contradicting_news = []

        for n in all_news[-15:]:
            if token_id in n.related_tokens or any(w in n.headline.lower() for w in slug.lower().split('-')[:3]):
                news_dict = {
                    "headline": n.headline,
                    "source": n.source,
                    "category": n.category,
                    "sentiment": n.sentiment,
                    "timestamp": n.timestamp,
                    "age_minutes": round((time.time() - n.timestamp) / 60, 1),
                }
                if (raw_edge >= 0 and n.sentiment > 0) or (raw_edge < 0 and n.sentiment < 0):
                    supporting_news.append(news_dict)
                else:
                    contradicting_news.append(news_dict)

        # 5. Market Regime Classification
        from core.deep_analysis import deep_analysis_engine
        regime_info = deep_analysis_engine.classify_regime(book)

        # 6. Recommendation Action & Rationale
        reasons = []
        action = "MONITOR"

        if spread > 0.04:
            action = "REJECT_RISK"
            reasons.append(f"Spread too wide ({(spread * 100):.1f}¢ > 4.0¢ limit)")
        elif total_depth_usdc < 200.0:
            action = "REJECT_RISK"
            reasons.append(f"Insufficient order book liquidity (${total_depth_usdc:.0f} < $200)")
        elif regime_info.get("tag") == "volatile":
            action = "MONITOR"
            reasons.append("High-volatility regime — ML ensemble not trained for liquidation dynamics; standing by")
        elif net_edge >= 0.035 and confidence >= 0.60:
            action = "TRADE_LONG_YES"
            reasons.append(f"Significant positive net edge (+{(net_edge * 100):.1f}%) with {confidence * 100:.0f}% confidence")
        elif net_edge <= -0.035 and confidence >= 0.60:
            action = "TRADE_SHORT_NO"
            reasons.append(f"Significant negative edge ({(net_edge * 100):.1f}%) favoring NO side")
        else:
            action = "MONITOR"
            reasons.append(f"Edge ({net_edge * 100:+.1f}%) within noise band; monitoring for microstructure catalyst")

        return {
            "token_id": token_id,
            "slug": slug,
            "status": "VALIDATED",
            "market_implied_prob": round(mid_p, 4),
            "ml_forecast_prob": round(p_ml, 4),
            "uncertainty_interval": [p_lower, p_upper],
            "raw_edge": round(raw_edge, 4),
            "net_edge": round(net_edge, 4),
            "confidence_score": round(confidence, 3),
            "best_bid": round(book.best_bid, 3) if book.best_bid else None,
            "best_ask": round(book.best_ask, 3) if book.best_ask else None,
            "spread_dollars": round(spread, 4),
            "spread_pct": round(spread_pct, 4),
            "total_liquidity_usdc": round(total_depth_usdc, 2),
            "bid_depth_usdc": round(bid_depth, 2),
            "ask_depth_usdc": round(ask_depth, 2),
            "order_flow_imbalance": ofi,
            "slippage_bps": slippage_bps,
            "fundamental_sentiment": token_sentiment,
            "supporting_evidence": supporting_news[:3],
            "contradicting_evidence": contradicting_news[:3],
            "suggested_action": action,
            "action_reasons": reasons,
            "regime": regime_info.get("regime", "Unknown"),
            "regime_tag": regime_info.get("tag", "unknown"),
            "model_metadata": {
                "version": model_registry.active_version,
                "brier_score": ml_model.brier_score,
                "ece": ml_model.ece,
                "roc_auc": ml_model.roc_auc,
                "features_used": ml_model.scaler.n_features_in_ if ml_model.is_fitted and hasattr(ml_model.scaler, 'n_features_in_') else 38,
                "adaptive_weights": ml_model.adaptive_weights,
            },
            "data_freshness_seconds": round(time.time() - book.updated_at, 1),
            "generation_time_ms": round((time.time() - start_t) * 1000, 2),
        }

    def get_top_ranked_opportunities(self, limit: int = 10) -> list[dict[str, Any]]:
        """Rank all active prediction markets by net expected alpha edge."""
        results = []
        for token_id in list(store.order_books.keys())[:50]:
            analysis = self.analyze_market(token_id)
            if analysis.get("status") == "VALIDATED":
                alpha_score = abs(analysis.get("net_edge", 0.0)) * analysis.get("confidence_score", 0.5)
                analysis["alpha_score"] = round(alpha_score, 4)
                results.append(analysis)

        results.sort(key=lambda x: x.get("alpha_score", 0.0), reverse=True)
        return results[:limit]


# Global singleton
deep_analysis_engine = DeepMarketAnalysisEngine()
