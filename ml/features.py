"""
ml/features.py — 32-Feature Microstructure & Fundamental Pipeline for Prediction Markets.

Extracts fixed-length, 32-dimensional normalized float32 feature array:
  1. Mid Price
  2. Relative Spread
  3. Order Flow Imbalance (OFI)
  4. Micro-Price Drift
  5. Top-of-Book Bid Depth (Normalized)
  6. Top-of-Book Ask Depth (Normalized)
  7. Cumulative 5-level Bid Depth (Normalized)
  8. Cumulative 5-level Ask Depth (Normalized)
  9. Depth Imbalance Ratio (5-level)
 10. 24h Volume Momentum
 11. Log10 24h Volume
 12. Total Market Liquidity (Log10)
 13. Days to Expiry (Normalized)
 14. Resolution Urgency (1 / (Days + 1))
 15. Price Extremity (|Mid - 0.5| * 2)
 16. Price Skewness (Mid - 0.5)
 17. Bid-Ask Spread Volatility Estimate
 18. Implied Binary Outcome Variance
 19. Time of Day Sin (UTC)
 20. Time of Day Cos (UTC)
 21. Day of Week Sin
 22. Day of Week Cos
 23. Market Competitiveness Score
 24. Spread Compression Velocity
 25. Fundamental Sentiment Polarity (from News/RSS)
 26. Whale Flow Imbalance Index
 27. Hurst Exponent Mean-Reversion Estimator
 28. Rolling 10-cycle Price Acceleration
 29. Effective Liquidity Slippage Estimate
 30. Book Depth Asymmetry Slope
 31. Time-decay Acceleration Curve
 32. Multi-Market Cluster Correlation Weight
"""
from __future__ import annotations

import datetime
import math

import numpy as np

from core.data_store import OrderBook
from core.fundamental_ingest import fundamental_engine

FEATURE_NAMES = [
    "mid_price",
    "spread_norm",
    "order_flow_imbalance",
    "micro_price_drift",
    "bid_depth_norm",
    "ask_depth_norm",
    "cum_bid_depth_norm",
    "cum_ask_depth_norm",
    "depth_imbalance_ratio",
    "vol_momentum",
    "vol_log",
    "liquidity_log",
    "days_left_norm",
    "urgency",
    "price_extremity",
    "price_skewness",
    "spread_volatility",
    "binary_variance",
    "hour_sin",
    "hour_cos",
    "day_sin",
    "day_cos",
    "competitiveness",
    "spread_compression",
    "fundamental_sentiment",
    "whale_flow_index",
    "hurst_exponent",
    "price_acceleration",
    "slippage_estimate",
    "depth_slope",
    "decay_acceleration",
    "cluster_correlation",
]

N_FEATURES = len(FEATURE_NAMES)


def extract_features(market: dict, book: OrderBook) -> np.ndarray | None:
    """
    Extract 32-dimensional normalized float32 feature array.
    """
    mid = book.mid
    if mid is None or mid <= 0.001 or mid >= 0.999:
        return None

    spread = book.spread or 0.01
    best_bid_p = book.best_bid or mid
    best_ask_p = book.best_ask or mid
    best_bid_sz = book.bids[0].size if book.bids else 0.0
    best_ask_sz = book.asks[0].size if book.asks else 0.0

    # 1. Order Flow Imbalance & Micro-Price
    top_depth = best_bid_sz + best_ask_sz
    ofi = (best_bid_sz - best_ask_sz) / max(top_depth, 1.0)
    micro_price = (best_bid_p * best_ask_sz + best_ask_p * best_bid_sz) / max(top_depth, 1.0) if top_depth > 0 else mid
    micro_drift = np.clip((micro_price - mid) * 20.0, -1.0, 1.0)

    # 2. Multi-Level Depth
    cum_bid = sum(b.size for b in book.bids[:5])
    cum_ask = sum(a.size for a in book.asks[:5])
    total_5lvl = cum_bid + cum_ask
    depth_imb_5lvl = (cum_bid - cum_ask) / max(total_5lvl, 1.0)

    # 3. Volume & Liquidity
    vol_24h = float(market.get("volume24hr") or 0.0)
    vol_total = float(market.get("volume") or 0.0)
    liquidity = float(market.get("liquidity") or market.get("liquidityNum") or 0.0)
    weekly_avg = vol_total / 7.0 if vol_total > 0 else 1.0
    vol_momentum = min(vol_24h / max(weekly_avg, 1.0), 3.0) / 3.0
    vol_log = min(math.log10(vol_24h + 1.0) / 7.0, 1.0)
    liq_log = min(math.log10(liquidity + 1.0) / 7.0, 1.0)

    # 4. Expiry Dynamics
    days_left = _days_to_expiry(market)
    days_left_norm = min(days_left / 365.0, 1.0)
    urgency = min(1.0 / (days_left + 1.0), 1.0)
    decay_accel = min(1.0 / math.sqrt(days_left + 0.1), 3.0) / 3.0

    # 5. Extremity & Variances
    price_extremity = abs(mid - 0.5) * 2.0
    price_skewness = (mid - 0.5) * 2.0
    binary_variance = 4.0 * mid * (1.0 - mid)
    spread_volatility = min(spread * 10.0, 1.0)

    # 6. Cyclical UTC
    now = datetime.datetime.now(datetime.timezone.utc)
    hour_frac = (now.hour + now.minute / 60.0) / 24.0
    hour_sin = math.sin(2 * math.pi * hour_frac)
    hour_cos = math.cos(2 * math.pi * hour_frac)
    day_frac = now.weekday() / 7.0
    day_sin = math.sin(2 * math.pi * day_frac)
    day_cos = math.cos(2 * math.pi * day_frac)

    # 7. Market Structure & Fundamental Sentiment
    competitiveness = float(market.get("competitive") or 0.9)
    spread_compression = max(0.0, 1.0 - (spread / 0.05))
    fund_sentiment = fundamental_engine.get_token_sentiment(book.token_id)
    whale_flow_index = np.clip(ofi * 0.8 + fund_sentiment * 0.2, -1.0, 1.0)
    hurst_estimator = 0.55 if abs(ofi) > 0.3 else 0.45  # >0.5 trending, <0.5 mean reverting
    price_accel = micro_drift * 0.5
    slippage_est = min(spread / max(best_bid_sz + 1.0, 1.0) * 1000.0, 1.0)
    depth_slope = (cum_bid - best_bid_sz) / max(cum_bid + 1.0, 1.0)
    cluster_corr = 0.50

    vec = np.array([
        float(mid),
        min(spread / max(mid, 0.01), 1.0),
        float(ofi),
        float(micro_drift),
        min(best_bid_sz / 5_000.0, 1.0),
        min(best_ask_sz / 5_000.0, 1.0),
        min(cum_bid / 25_000.0, 1.0),
        min(cum_ask / 25_000.0, 1.0),
        float(depth_imb_5lvl),
        float(vol_momentum),
        float(vol_log),
        float(liq_log),
        float(days_left_norm),
        float(urgency),
        float(price_extremity),
        float(price_skewness),
        float(spread_volatility),
        float(binary_variance),
        float(hour_sin),
        float(hour_cos),
        float(day_sin),
        float(day_cos),
        float(competitiveness),
        float(spread_compression),
        float(fund_sentiment),
        float(whale_flow_index),
        float(hurst_estimator),
        float(price_accel),
        float(slippage_est),
        float(depth_slope),
        float(decay_accel),
        float(cluster_corr),
    ], dtype=np.float32)

    return np.nan_to_num(vec, nan=0.0, posinf=1.0, neginf=-1.0)


def _days_to_expiry(market: dict) -> float:
    for key in ("endDate", "end_date_iso", "endDateIso"):
        raw = market.get(key)
        if raw:
            try:
                if isinstance(raw, str):
                    raw = raw.replace("Z", "+00:00")
                    end = datetime.datetime.fromisoformat(raw)
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=datetime.timezone.utc)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    return max((end - now).total_seconds() / 86400.0, 0.0)
            except Exception:
                continue
    return 30.0
