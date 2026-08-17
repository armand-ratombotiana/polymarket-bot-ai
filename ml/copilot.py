"""
ml/copilot.py — GenAI Market Intelligence & Copilot Engine.

Provides:
  - Natural-language market analysis and trade rationale generation.
  - Semantic Q&A over active Polymarket prediction markets.
  - Automated risk briefing & volatility commentary.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from core.data_store import store
from ml.features import extract_features
from ml.model import ml_model
from ml.vector_store import vector_store

log = logging.getLogger(__name__)


class AICopilotEngine:
    """
    GenAI market reasoning & trading copilot assistant.
    """

    def __init__(self) -> None:
        self._history: List[dict] = []

    async def analyze_market(self, token_id: str, market_dict: Optional[dict] = None) -> dict:
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
                "recommendation": "Hold",
                "risk_score": "Medium",
            }

        mid = book.mid or 0.5
        spread = book.spread or 0.01
        p_yes, conf = 0.5, 0.0

        if market_dict:
            feats = extract_features(market_dict, book)
            if feats is not None:
                p_yes, conf = ml_model.predict(feats)

        # Microstructure heuristics
        best_bid_sz = book.bids[0].size if book.bids else 0.0
        best_ask_sz = book.asks[0].size if book.asks else 0.0
        depth_imbalance = (best_bid_sz - best_ask_sz) / max(best_bid_sz + best_ask_sz, 1.0)

        # Directional recommendation
        if p_yes >= 0.58 and depth_imbalance > 0.1:
            rec = "Strong Buy (YES)"
            sentiment = "Bullish"
            risk = "Low" if spread < 0.02 else "Medium"
        elif p_yes <= 0.42 and depth_imbalance < -0.1:
            rec = "Strong Sell (YES) / Buy NO"
            sentiment = "Bearish"
            risk = "Low" if spread < 0.02 else "Medium"
        elif spread > 0.05:
            rec = "Market Make / Capture Spread"
            sentiment = "Volatile"
            risk = "High"
        else:
            rec = "Neutral / Hold"
            sentiment = "Balanced"
            risk = "Low"

        rationale = (
            f"Market {slug} is pricing at {mid*100:.1f}¢ with spread {spread*100:.1f}¢. "
            f"Ensemble ML model projects win probability at {p_yes:.1%} (confidence: {conf*100:.0f}%). "
            f"Order book displays a {depth_imbalance*100:+.1f}% depth imbalance toward the {'bid' if depth_imbalance > 0 else 'ask'} side."
        )

        return {
            "token_id": token_id,
            "slug": slug,
            "mid_price": round(mid, 4),
            "spread": round(spread, 4),
            "ml_probability": round(p_yes, 4),
            "confidence": round(conf, 4),
            "sentiment": sentiment,
            "recommendation": rec,
            "risk_score": risk,
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
            matched_markets = [
                {
                    "token_id": meta["token_id"],
                    "title": meta["title"],
                    "slug": meta["slug"],
                    "similarity": score,
                }
                for meta, score in results
            ]
            top_match = matched_markets[0]
            reply = (
                f"Based on semantic indexing, I identified relevant market **{top_match['title']}** "
                f"(relevance: {top_match['similarity']*100:.1f}%). "
                f"Our ML ensemble and arbitrage models are active across these contracts. "
                "Would you like an order book depth breakdown or to execute a strategy quote?"
            )

        return {
            "query": user_query,
            "reply": reply,
            "matched_markets": matched_markets,
            "timestamp": time.time(),
        }


# Global singleton
copilot_engine = AICopilotEngine()
