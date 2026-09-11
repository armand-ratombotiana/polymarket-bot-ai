"""Market sentiment analyzer — aggregates news + social signals.

Sources:
- News articles (via fundamental_ingest)
- GDELT events (if available)
- Price action (bullish/bearish momentum)
- Market volume trends

Output: per-token sentiment score (-1.0 to +1.0) with confidence.

W17-1 — Aggregates heterogeneous sentiment signals (news keyword
polarity, price-action momentum, volume-trend conviction, social)
into a single per-token ``AggregatedSentiment`` with a confidence
weight + an improving/declining/stable trend label. Backed by a
dedicated SQLite DB (``SENTIMENT_DB_PATH``, defaulting to
``/app/data/sentiment.db`` — same convention as
``core/decision_ledger`` / ``core.observability`` / ``core.audit_logger``
so a sentiment write never perturbs the audit-trail / decision-ledger
DBs). All writes are fire-and-forget from the caller's perspective
(swallowed at the sqlite layer) so an analyzer hiccup can never break
the trading pipeline — mirrors the ``decision_ledger`` /
``observability`` contracts.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SENTIMENT_DB_PATH = Path(os.environ.get("SENTIMENT_DB_PATH", "/app/data/sentiment.db"))


@dataclass
class SentimentSignal:
    """A single sentiment observation from one source at one instant.

    ``source`` is one of: ``"news"``, ``"gdelt"``, ``"price"``,
    ``"volume"``, ``"social"``. ``score`` is the polarity in
    ``[-1.0, +1.0]``; ``confidence`` in ``[0.0, 1.0]`` reflects how
    much weight the aggregator should give this signal relative to
    others from the same source.
    """

    source: str  # "news", "gdelt", "price", "volume", "social"
    token_id: str
    score: float  # -1.0 to +1.0
    confidence: float  # 0.0 to 1.0
    timestamp: float
    metadata: dict = field(default_factory=dict)


@dataclass
class AggregatedSentiment:
    """Weighted aggregate of every signal recorded for a token."""

    token_id: str
    overall_score: float  # -1.0 to +1.0
    confidence: float
    signal_count: int
    breakdown: dict  # {source: score}
    trend: str  # "improving", "declining", "stable"
    updated_at: float


class SentimentAnalyzer:
    """Aggregates sentiment signals from multiple sources.

    The analyzer is intentionally stateless across process restarts
    beyond its on-disk SQLite store: every ``record_signal`` call
    writes one row to ``sentiment_signals`` and every ``aggregate``
    call reads the rows back, weights by confidence + recency, and
    UPSERTs the result into ``aggregated_sentiment`` so a dashboard
    poll against ``GET /api/sentiment`` is O(1) in the number of
    tracked tokens.
    """

    def __init__(self, db_path: Path = SENTIMENT_DB_PATH):
        self._db_path = db_path
        self._init_db()
        self._cache: dict[str, AggregatedSentiment] = {}

    def _init_db(self):
        """Create tables / indexes if absent.

        Wrapped in a try/except so an unwritable ``/app/data`` (the
        sandbox default — raises ``PermissionError`` on the parent-
        dir ``mkdir`` before ``sqlite3.connect`` ever runs) does not
        crash the module import — mirrors the
        ``core.decision_ledger._init_db`` defensive pattern. Both
        ``OSError`` (from ``Path.mkdir`` / ``sqlite3.connect``) and
        ``sqlite3.Error`` are swallowed.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sentiment_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        token_id TEXT NOT NULL,
                        score REAL NOT NULL,
                        confidence REAL NOT NULL,
                        timestamp REAL NOT NULL,
                        metadata TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_ss_token_ts ON sentiment_signals(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_ss_source ON sentiment_signals(source);

                    CREATE TABLE IF NOT EXISTS aggregated_sentiment (
                        token_id TEXT PRIMARY KEY,
                        overall_score REAL NOT NULL,
                        confidence REAL NOT NULL,
                        signal_count INTEGER NOT NULL,
                        breakdown TEXT NOT NULL,
                        trend TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
        except (OSError, sqlite3.Error) as exc:  # pragma: no cover — defensive
            logger.warning("sentiment DB init failed (%s): %s", self._db_path, exc)

    def record_signal(self, signal: SentimentSignal):
        """Persist one signal. Swallows persistence errors so an
        analyzer hiccup never breaks the caller's request path."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO sentiment_signals (source, token_id, score, confidence, timestamp, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.source,
                        signal.token_id,
                        signal.score,
                        signal.confidence,
                        signal.timestamp,
                        str(signal.metadata),
                    ),
                )
        except sqlite3.Error as exc:  # pragma: no cover — defensive
            logger.warning("sentiment record_signal failed: %s", exc)

    def analyze_news(self, text: str, token_id: str) -> SentimentSignal:
        """Simple keyword-based news sentiment analysis.

        Mirrors ``core.fundamental_ingest.FundamentalIngestionEngine.score_text``
        in spirit (bullish-vs-bearish keyword tally) but returns the
        full :class:`SentimentSignal` envelope (so the analyzer can
        record the score + a confidence derived from the keyword
        density in one shot).
        """
        text_lower = text.lower()

        # Bullish keywords
        bullish = [
            "surge", "rally", "gain", "bullish", "positive", "upside", "breakout",
            "support", "buy", "long", "optimistic", "growth", "bull", "soar",
        ]
        # Bearish keywords
        bearish = [
            "crash", "plunge", "decline", "bearish", "negative", "downside",
            "breakdown", "resistance", "sell", "short", "pessimistic", "loss",
            "bear", "drop", "fall",
        ]

        bull_count = sum(1 for kw in bullish if kw in text_lower)
        bear_count = sum(1 for kw in bearish if kw in text_lower)

        total = bull_count + bear_count
        if total == 0:
            score = 0.0
            confidence = 0.1
        else:
            score = (bull_count - bear_count) / total
            confidence = min(total / 10.0, 1.0)  # More keywords = higher confidence

        return SentimentSignal(
            source="news",
            token_id=token_id,
            score=score,
            confidence=confidence,
            timestamp=time.time(),
            metadata={"bull_count": bull_count, "bear_count": bear_count, "text_length": len(text)},
        )

    def analyze_price_action(self, prices: list[float], token_id: str) -> SentimentSignal:
        """Derive sentiment from price action (momentum).

        ``prices`` is a chronological list of recent prices. The
        analyzer computes the percent change from the first to the
        last sample, normalises to ``[-1.0, +1.0]`` at ±5 %, and
        scales confidence by sample count up to 20 samples.
        """
        if len(prices) < 2:
            return SentimentSignal("price", token_id, 0.0, 0.0, time.time())

        recent = prices[-1]
        past = prices[0]
        change = (recent - past) / past if past > 0 else 0

        # Normalize: ±5% = ±1.0 sentiment
        score = max(-1.0, min(1.0, change / 0.05))
        confidence = min(len(prices) / 20.0, 1.0)

        return SentimentSignal(
            source="price",
            token_id=token_id,
            score=score,
            confidence=confidence,
            timestamp=time.time(),
            metadata={"price_change_pct": change * 100, "n_prices": len(prices)},
        )

    def analyze_volume(self, volumes: list[float], token_id: str) -> SentimentSignal:
        """Derive sentiment from volume trends.

        Unusual volume (recent avg ≥ 2 × past avg) is mildly positive
        (conviction); volume drying up (≤ 0.5 × past avg) is mildly
        negative (apathy / unwind). Neutral otherwise.
        """
        if len(volumes) < 2:
            return SentimentSignal("volume", token_id, 0.0, 0.0, time.time())

        avg_recent = sum(volumes[-5:]) / min(5, len(volumes))
        avg_past = sum(volumes[:-5]) / max(1, len(volumes) - 5)

        vol_ratio = 1.0
        if avg_past == 0:
            score = 0.0
        else:
            vol_ratio = avg_recent / avg_past
            score = 0.0  # Neutral — volume amplifies, doesn't direct
            if vol_ratio > 2.0:
                score = 0.3  # Unusual volume = mild positive
            elif vol_ratio < 0.5:
                score = -0.3  # Volume drying up = mild negative

        confidence = min(len(volumes) / 10.0, 1.0)
        return SentimentSignal(
            source="volume",
            token_id=token_id,
            score=score,
            confidence=confidence,
            timestamp=time.time(),
            metadata={"vol_ratio": vol_ratio},
        )

    def aggregate(self, token_id: str, lookback_hours: float = 24) -> AggregatedSentiment:
        """Aggregate all sentiment signals for a token.

        Weighting is ``score × recency_weight × confidence_weight``,
        where ``recency_weight`` linearly decays from 1.0 (now) to
        0.0 (``lookback_hours`` ago). The aggregate confidence is the
        total weight clamped to ``[0, 1]`` at a 10-unit scale.
        """
        cutoff = time.time() - lookback_hours * 3600
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM sentiment_signals WHERE token_id = ? AND timestamp > ? ORDER BY timestamp DESC",
                    (token_id, cutoff),
                ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover — defensive
            logger.warning("sentiment aggregate read failed: %s", exc)
            rows = []

        if not rows:
            return AggregatedSentiment(
                token_id=token_id,
                overall_score=0.0,
                confidence=0.0,
                signal_count=0,
                breakdown={},
                trend="stable",
                updated_at=time.time(),
            )

        # Weight by confidence and recency
        now = time.time()
        total_weight = 0.0
        weighted_score = 0.0
        breakdown: dict[str, list[float]] = {}

        for row in rows:
            age_hours = (now - row["timestamp"]) / 3600
            recency_weight = max(0, 1 - age_hours / lookback_hours)
            confidence_weight = row["confidence"]
            weight = recency_weight * confidence_weight

            weighted_score += row["score"] * weight
            total_weight += weight

            source = row["source"]
            if source not in breakdown:
                breakdown[source] = []
            breakdown[source].append(row["score"])

        overall = weighted_score / total_weight if total_weight > 0 else 0.0

        # Compute breakdown averages
        breakdown_avg = {source: sum(scores) / len(scores) for source, scores in breakdown.items()}

        # Determine trend (compare recent vs older signals)
        if len(rows) >= 4:
            recent_half = rows[: len(rows) // 2]
            older_half = rows[len(rows) // 2:]
            recent_avg = sum(r["score"] for r in recent_half) / len(recent_half)
            older_avg = sum(r["score"] for r in older_half) / len(older_half)
            diff = recent_avg - older_avg
            if diff > 0.1:
                trend = "improving"
            elif diff < -0.1:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        result = AggregatedSentiment(
            token_id=token_id,
            overall_score=overall,
            confidence=min(total_weight / 10.0, 1.0),
            signal_count=len(rows),
            breakdown=breakdown_avg,
            trend=trend,
            updated_at=now,
        )

        # Cache and persist
        self._cache[token_id] = result
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO aggregated_sentiment
                    (token_id, overall_score, confidence, signal_count, breakdown, trend, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (token_id, overall, result.confidence, len(rows), str(breakdown_avg), trend, now),
                )
        except sqlite3.Error as exc:  # pragma: no cover — defensive
            logger.warning("sentiment aggregate persist failed: %s", exc)

        return result

    def get_all_sentiment(self) -> list[dict]:
        """Return every aggregated-sentiment row, highest score first.

        Used by the ``GET /api/sentiment`` dashboard endpoint. Each
        row's ``breakdown`` is a ``str(metadata)`` snapshot from the
        aggregator — the route layer returns it as-is for the
        dashboard to render.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM aggregated_sentiment ORDER BY overall_score DESC"
                ).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error as exc:  # pragma: no cover — defensive
            logger.warning("sentiment get_all_sentiment failed: %s", exc)
            return []


sentiment_analyzer = SentimentAnalyzer()


# ── Pydantic request model (module scope) ────────────────────────────────────
# Defined at module scope (not inside ``register_routes``) because FastAPI's
# signature inspection at decoration time can mis-classify a locally-defined
# BaseModel as a query parameter — producing a 422 "Field required" on the
# ``body`` query parameter instead of binding the request body. Mirrors the
# pattern in ``core/portfolio_optimizer.py`` (OptimizeRequest /
# RebalanceRequest / ConfigUpdate at module scope).
try:
    from pydantic import BaseModel, Field

    class AnalyzeRequest(BaseModel):
        """Request body for ``POST /api/sentiment/analyze``."""

        text: str = Field(..., min_length=1, description="News text / social post to analyze")
        token_id: str = Field(..., min_length=1, description="Token the text refers to")
except ImportError:  # pragma: no cover — defensive: FastAPI / pydantic optional
    AnalyzeRequest = None  # type: ignore[assignment,misc]


# ── FastAPI route registration ──────────────────────────────────────────────


def register_routes(app: Any) -> None:
    """Append the sentiment-analysis endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET  /api/sentiment/{token_id}
                                aggregated sentiment for one token
                                (overall score / confidence / signal
                                count / per-source breakdown / trend).
                                Triggers a fresh ``aggregate`` call so
                                the response reflects the latest
                                recorded signals.
      GET  /api/sentiment        every aggregated-sentiment row
                                (highest score first), as persisted by
                                the most recent ``aggregate`` calls.
      POST /api/sentiment/analyze
                                analyze a text blob for keyword
                                sentiment, record the resulting
                                signal, and return the freshly-
                                aggregated sentiment for that token.
    """
    from fastapi import (
        HTTPException,  # local import — FastAPI is optional at module load
    )

    @app.get("/api/sentiment/{token_id}", tags=["sentiment"])
    async def _get_sentiment(token_id: str):
        """Return the aggregated sentiment for ``token_id``.

        Triggers a fresh ``aggregate`` (24h lookback) so the response
        reflects every signal recorded up to this call. Returns 200
        even when no signals exist (zeroed-out envelope).
        """
        result = sentiment_analyzer.aggregate(token_id, lookback_hours=24.0)
        return {
            "token_id": result.token_id,
            "overall_score": result.overall_score,
            "confidence": result.confidence,
            "signal_count": result.signal_count,
            "breakdown": result.breakdown,
            "trend": result.trend,
            "updated_at": result.updated_at,
        }

    @app.get("/api/sentiment", tags=["sentiment"])
    async def _list_sentiment():
        """Return every persisted aggregate (highest score first)."""
        return {"sentiments": sentiment_analyzer.get_all_sentiment()}

    @app.post("/api/sentiment/analyze", tags=["sentiment"])
    async def _analyze_text(body: AnalyzeRequest):
        """Analyze ``text`` for keyword sentiment, record the signal,
        and return the freshly-aggregated sentiment for ``token_id``.

        Returns the same envelope shape as
        ``GET /api/sentiment/{token_id}`` so a client can poll either
        endpoint interchangeably after a write.
        """
        if not body.text or not body.text.strip():
            raise HTTPException(status_code=422, detail="text must not be empty")
        signal = sentiment_analyzer.analyze_news(body.text, body.token_id)
        sentiment_analyzer.record_signal(signal)
        result = sentiment_analyzer.aggregate(body.token_id, lookback_hours=24.0)
        return {
            "token_id": result.token_id,
            "overall_score": result.overall_score,
            "confidence": result.confidence,
            "signal_count": result.signal_count,
            "breakdown": result.breakdown,
            "trend": result.trend,
            "updated_at": result.updated_at,
            "signal": {
                "source": signal.source,
                "score": signal.score,
                "confidence": signal.confidence,
                "metadata": signal.metadata,
            },
        }


__all__ = [
    "SENTIMENT_DB_PATH",
    "SentimentSignal",
    "AggregatedSentiment",
    "SentimentAnalyzer",
    "sentiment_analyzer",
    "AnalyzeRequest",
    "register_routes",
]
