"""
ml/copilot.py — GenAI Market Intelligence & Copilot Engine.

Provides:
  - Natural-language market analysis and trade rationale generation.
  - Semantic Q&A over active Polymarket prediction markets.
  - Automated risk briefing, regime context & volatility commentary.
"""
from __future__ import annotations

import logging
import time

from core.data_store import store
from ml.drift_detector import drift_detector
from ml.features import FEATURE_NAMES, extract_features
from ml.model import ml_model
from ml.model_registry import model_registry
from ml.vector_store import vector_store

log = logging.getLogger(__name__)


class AICopilotEngine:
    """
    GenAI market reasoning & trading copilot assistant.
    """

    def __init__(self) -> None:
        self._history: list[dict] = []

    async def analyze_market(self, token_id: str, market_dict: dict | None = None) -> dict:
        """
        Produce a comprehensive quant & fundamental briefing for a given market token.
        """
        book = await store.get_order_book(token_id)
        slug = store.market_slugs.get(token_id, token_id[:16])

        if not book:
            return {
                "token_id": token_id,
                "slug": slug,
                "summary": f"Order book data currently initializing for {slug}.",
                "sentiment": "Neutral",
                "ml_probability": 0.50,
                "net_edge": 0.0,
                "confidence": 0.0,
                "regime": "Unknown",
                "regime_tag": "unknown",
                "recommendation": "Hold",
                "risk_score": "Medium",
                "feature_drivers": [],
                "model_metadata": {
                    "version": model_registry.active_version,
                    "brier_score": ml_model.brier_score,
                    "ece": ml_model.ece,
                    "drift_status": drift_detector.drift_status,
                },
            }

        mid = book.mid or 0.5
        spread = book.spread or 0.01
        p_yes, conf = 0.5, 0.0
        feature_drivers: list[dict[str, float]] = []

        # Auto-fetch market metadata from catalog if not provided
        if not market_dict:
            try:
                from core.market_discovery import market_discovery
                market_dict = market_discovery.catalog.get(token_id) or {"slug": slug}
            except Exception:
                market_dict = {"slug": slug}

        feats = extract_features(market_dict, book)
        if feats is not None and ml_model.is_fitted:
            p_yes, conf = ml_model.predict(feats, token_id=token_id)

            # Identify top feature drivers for this prediction
            if ml_model.feature_importances:
                # Weight feature importance by the feature's deviation/activation
                scored_features = []
                for name, imp in ml_model.feature_importances.items():
                    if name in FEATURE_NAMES:
                        idx = FEATURE_NAMES.index(name)
                        feat_val = float(feats[idx])
                        scored_features.append({
                            "feature": name,
                            "importance": imp,
                            "value": round(feat_val, 4),
                            "impact": round(imp * abs(feat_val), 4),
                        })
                scored_features.sort(key=lambda x: x["impact"], reverse=True)
                feature_drivers = scored_features[:3]

        # Microstructure heuristics & regime
        best_bid_sz = book.bids[0].size if book.bids else 0.0
        best_ask_sz = book.asks[0].size if book.asks else 0.0
        depth_imbalance = (best_bid_sz - best_ask_sz) / max(best_bid_sz + best_ask_sz, 1.0)

        # Classify regime
        from core.deep_analysis import deep_analysis_engine
        regime_info = deep_analysis_engine.classify_regime(book)
        regime_tag = regime_info.get("tag", "unknown")
        regime_name = regime_info.get("regime", "Unknown")

        # Directional recommendation
        net_edge = p_yes - mid
        if regime_tag == "volatile":
            rec = "Stand By (High Volatility)"
            sentiment = "Volatile"
            risk = "High"
        elif p_yes >= 0.55 and net_edge >= 0.035:
            rec = "Strong Buy (YES)"
            sentiment = "Bullish"
            risk = "Low" if spread < 0.02 else "Medium"
        elif p_yes <= 0.45 and net_edge <= -0.035:
            rec = "Strong Sell (YES) / Buy NO"
            sentiment = "Bearish"
            risk = "Low" if spread < 0.02 else "Medium"
        elif spread > 0.04:
            rec = "Market Make / Capture Spread"
            sentiment = "Wide Spread"
            risk = "Medium"
        else:
            rec = "Neutral / Monitor"
            sentiment = "Balanced"
            risk = "Low"

        drivers_summary = ""
        if feature_drivers:
            d_strs = [f"{d['feature']} ({d['value']:+.2f})" for d in feature_drivers]
            drivers_summary = f" Key driver signals: {', '.join(d_strs)}."

        rationale = (
            f"Market {slug} is pricing at {mid*100:.1f}¢ (spread: {spread*100:.1f}¢, regime: {regime_name}). "
            f"Ensemble ML model projects win probability at {p_yes:.1%} (edge: {net_edge*100:+.1f}%, confidence: {conf*100:.0f}%). "
            f"Order book depth imbalance is {depth_imbalance*100:+.1f}% toward the {'bid' if depth_imbalance > 0 else 'ask'} side."
            f"{drivers_summary}"
        )

        return {
            "token_id": token_id,
            "slug": slug,
            "mid_price": round(mid, 4),
            "spread": round(spread, 4),
            "ml_probability": round(p_yes, 4),
            "net_edge": round(net_edge, 4),
            "confidence": round(conf, 4),
            "regime": regime_name,
            "regime_tag": regime_tag,
            "sentiment": sentiment,
            "recommendation": rec,
            "risk_score": risk,
            "feature_drivers": feature_drivers,
            "model_metadata": {
                "version": model_registry.active_version,
                "brier_score": ml_model.brier_score,
                "ece": ml_model.ece,
                "drift_status": drift_detector.drift_status,
                "adaptive_weights": ml_model.adaptive_weights,
            },
            "rationale": rationale,
            "generated_at": time.time(),
        }

    async def answer_query(self, user_query: str) -> dict:
        """
        Process a conversational query using semantic market retrieval + quant context.
        """
        results = vector_store.search(user_query, top_k=4)

        if not results:
            # Fallback to general market scan
            top_books = list(store.order_books.values())[:3]
            sample_slugs = [store.market_slugs.get(b.token_id, "market") for b in top_books]
            reply = (
                f"I am actively monitoring {len(store.order_books)} prediction markets. "
                f"Currently tracking volume leaders: {', '.join(sample_slugs)}. "
                "How can I help optimize your trading strategies or evaluate specific outcomes?"
            )
            matched_markets = []
        else:
            matched_markets = []
            for meta, score in results:
                tid = meta["token_id"]
                book = store.order_books.get(tid)
                mid_val = round(book.mid, 4) if book and book.mid is not None else None
                matched_markets.append({
                    "token_id": tid,
                    "title": meta["title"],
                    "slug": meta["slug"],
                    "mid_price": mid_val,
                    "similarity": round(score, 3),
                })

            top_match = matched_markets[0]
            top_mid_str = f" (current mid: {top_match['mid_price']*100:.1f}¢)" if top_match.get("mid_price") else ""
            reply = (
                f"Based on semantic indexing, I identified relevant market **{top_match['title']}**{top_mid_str} "
                f"(semantic match: {top_match['similarity']*100:.1f}%). "
                f"Our 4-member calibrated ML ensemble and high-frequency scanners are tracking these contracts. "
                "Would you like a quantitative feature breakdown or suggested trade action?"
            )

        return {
            "query": user_query,
            "reply": reply,
            "matched_markets": matched_markets,
            "timestamp": time.time(),
        }


# Global singleton
copilot_engine = AICopilotEngine()
