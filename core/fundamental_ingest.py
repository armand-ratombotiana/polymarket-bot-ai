"""
core/fundamental_ingest.py — Real-time News, Macro & Fundamental Sentiment Ingestion Engine.

Ingests breaking news headlines, macro reports, and political polling data.
Calculates entity-tagged sentiment scores and matches fundamental signals to active prediction markets.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional

from ml.vector_store import vector_store

log = logging.getLogger(__name__)

BULLISH_TERMS = {
    "surge", "gain", "win", "approval", "approved", "lead", "rally", "outperform",
    "boost", "cut", "easing", "dovish", "record", "victory", "bullish", "passes", "confirmed"
}

BEARISH_TERMS = {
    "drop", "fall", "lose", "rejection", "rejected", "trail", "crash", "underperform",
    "hike", "hawkish", "deficit", "defeat", "bearish", "fails", "investigation", "indicted"
}


class FundamentalNewsItem:
    def __init__(
        self,
        headline: str,
        source: str,
        category: str,
        timestamp: float,
        sentiment: float,
        related_tokens: List[str],
    ) -> None:
        self.headline = headline
        self.source = source
        self.category = category
        self.timestamp = timestamp
        self.sentiment = sentiment
        self.related_tokens = related_tokens

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "source": self.source,
            "category": self.category,
            "timestamp": self.timestamp,
            "sentiment": round(self.sentiment, 3),
            "related_tokens": self.related_tokens,
        }


class FundamentalIngestionEngine:
    """
    Continuous news, macro, and fundamental data feed ingester.
    """

    def __init__(self) -> None:
        self.news_feed: List[FundamentalNewsItem] = []
        self._running = False
        self._token_sentiment: Dict[str, float] = {}

    def score_text(self, text: str) -> float:
        """Calculate sentiment polarity in [-1.0, 1.0]."""
        words = re.findall(r"\b\w+\b", text.lower())
        if not words:
            return 0.0
        pos = sum(1 for w in words if w in BULLISH_TERMS)
        neg = sum(1 for w in words if w in BEARISH_TERMS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    async def ingest_news_item(self, headline: str, source: str, category: str) -> FundamentalNewsItem:
        """Ingest a news item, compute sentiment, and match with active markets."""
        sentiment = self.score_text(headline)
        ts = time.time()

        # Match to prediction markets via Vector Store
        matches = vector_store.search(headline, top_k=3)
        related_tokens = [m[0]["token_id"] for m in matches if m[1] > 0.15]

        item = FundamentalNewsItem(
            headline=headline,
            source=source,
            category=category,
            timestamp=ts,
            sentiment=sentiment,
            related_tokens=related_tokens,
        )

        self.news_feed.append(item)
        if len(self.news_feed) > 100:
            self.news_feed = self.news_feed[-100:]

        # Update market token sentiment
        for tid in related_tokens:
            curr = self._token_sentiment.get(tid, 0.0)
            self._token_sentiment[tid] = 0.7 * curr + 0.3 * sentiment

        return item

    def get_token_sentiment(self, token_id: str) -> float:
        return self._token_sentiment.get(token_id, 0.0)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Seed initial fundamental mock events
        await self.ingest_news_item("Federal Reserve signals potential rate cut considerations in upcoming FOMC meeting", "Reuters", "Macro")
        await self.ingest_news_item("Bitcoin maintains institutional inflows as ETF volume hits new monthly high", "Bloomberg", "Crypto")
        await self.ingest_news_item("New presidential polling aggregate shows tightened swing state margins", "FiveThirtyEight", "Politics")
        await self.ingest_news_item("UFC Main Event championship bout confirmed following successful official weigh-ins", "ESPN", "Sports")
        log.info("[fundamental_ingest] Engine started with %d seeded items", len(self.news_feed))

    async def stop(self) -> None:
        self._running = False


# Global singleton
fundamental_engine = FundamentalIngestionEngine()
