"""
ml/features.py — 38-Feature Microstructure, Fundamental & Regime Pipeline for Prediction Markets.

Extracts fixed-length, 38-dimensional normalized float32 feature array:
  1.  Mid Price
  2.  Relative Spread
  3.  Order Flow Imbalance (OFI)
  4.  Micro-Price Drift
  5.  Top-of-Book Bid Depth (Normalized)
  6.  Top-of-Book Ask Depth (Normalized)
  7.  Cumulative 5-level Bid Depth (Normalized)
  8.  Cumulative 5-level Ask Depth (Normalized)
  9.  Depth Imbalance Ratio (5-level)
 10.  24h Volume Momentum
 11.  Log10 24h Volume
 12.  Total Market Liquidity (Log10)
 13.  Days to Expiry (Normalized)
 14.  Resolution Urgency (1 / (Days + 1))
 15.  Price Extremity (|Mid - 0.5| * 2)
 16.  Price Skewness (Mid - 0.5)
 17.  Bid-Ask Spread Volatility Estimate
 18.  Implied Binary Outcome Variance
 19.  Time of Day Sin (UTC)
 20.  Time of Day Cos (UTC)
 21.  Day of Week Sin
 22.  Day of Week Cos
 23.  Market Competitiveness Score
 24.  Spread Compression Velocity
 25.  Fundamental Sentiment Polarity (from News/RSS)
 26.  Whale Flow Imbalance Index
 27.  Hurst Exponent (R/S Analysis — 60-bar rolling window)
 28.  Rolling 10-cycle Price Acceleration (3-bar)
 29.  Effective Liquidity Slippage Estimate
 30.  Book Depth Asymmetry Slope
 31.  Time-decay Acceleration Curve
 32.  Multi-Market Cluster Correlation Weight
 33.  Regime: Directional Trending (binary)
 34.  Regime: Mean-Reverting Range (binary)
 35.  Regime: High Volatility / Wide Spread (binary)
 36.  Regime: Resolution Convergence (binary)
 37.  Rolling Price Volatility (std of last 10 log-returns)
 38.  Price Momentum 5-bar
"""
from __future__ import annotations

import datetime
import math
from collections import deque
from typing import Deque

import numpy as np

from core.data_store import OrderBook

# Module-level rolling price history cache: token_id -> deque of mid prices (last 60)
# Increased from 20 → 60: Hurst exponent requires ≥32 data points for statistical validity;
# the 5-bar momentum and rolling-volatility features also benefit from a longer window.
_price_history: dict[str, Deque[float]] = {}
_HISTORY_LEN = 60

FEATURE_NAMES = [
    # ── Microstructure (1-18) ──────────────────────────────────────────────────
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
    # ── Cyclical time (19-22) ──────────────────────────────────────────────────
    "hour_sin",
    "hour_cos",
    "day_sin",
    "day_cos",
    # ── Market structure / fundamentals (23-32) ────────────────────────────────
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
    # ── Regime one-hot flags (33-36) ──────────────────────────────────────────
    "regime_trending",
    "regime_mean_reverting",
    "regime_volatile",
    "regime_resolution",
    # ── Extended price dynamics (37-38) ───────────────────────────────────────
    "rolling_volatility",
    "price_momentum_5bar",
]

N_FEATURES = len(FEATURE_NAMES)  # 38


def _rs_hurst(prices: list[float]) -> float:
    """
    Proper R/S (Rescaled Range) Hurst exponent estimator.
    H > 0.5 -> trending (momentum); H < 0.5 -> mean-reverting; H ~= 0.5 -> random walk.
    Requires at least 8 data points; falls back to 0.5 on insufficient data.
    Using a longer history window (60 bars) significantly improves estimate reliability.
    """
    n = len(prices)
    if n < 8:
        return 0.5
    arr = np.array(prices, dtype=np.float64)
    # Log returns to work in a well-defined space
    rets = np.diff(np.log(np.clip(arr, 1e-6, 1.0 - 1e-6)))
    if len(rets) < 4:
        return 0.5
    mean_ret = float(np.mean(rets))
    deviations = np.cumsum(rets - mean_ret)
    R = float(np.max(deviations) - np.min(deviations))
    S = float(np.std(rets, ddof=1))
    if S < 1e-10 or R <= 0:
        return 0.5
    rs = R / S
    h = math.log(rs) / math.log(n)
    return float(np.clip(h, 0.01, 0.99))


def _cluster_correlation(token_id: str, mid: float) -> float:
    """
    Fraction of live order-book markets whose mid price is within ±0.05 of this
    market's mid. High value means crowded price cluster => higher adverse selection.
    Returns value in [0, 1].
    """
    try:
        from core.data_store import store
        books = list(store.order_books.values())
        n = len(books)
        if n < 5:
            return 0.5
        neighbours = sum(
            1 for b in books
            if b.token_id != token_id and b.mid is not None and abs(b.mid - mid) <= 0.05
        )
        return float(min(neighbours / n, 1.0))
    except Exception:
        return 0.5


def _classify_regime(mid: float, spread: float, depth_imb: float) -> tuple[float, float, float, float]:
    """
    Inline regime classifier (mirrors deep_analysis.DeepMarketAnalysisEngine.classify_regime
    but dependency-free to avoid circular imports).

    Returns (trending, mean_reverting, volatile, resolution) as 0/1 binary flags.
    """
    if mid >= 0.92 or mid <= 0.08:
        return 0.0, 0.0, 0.0, 1.0   # resolution convergence
    if spread >= 0.04:
        return 0.0, 0.0, 1.0, 0.0   # high volatility / wide spread
    if abs(depth_imb) > 0.40:
        return 1.0, 0.0, 0.0, 0.0   # directional trending
    return 0.0, 1.0, 0.0, 0.0       # mean-reverting range


def extract_features(market: dict, book: OrderBook) -> np.ndarray | None:
    """
    Extract 38-dimensional normalized float32 feature array.
    """
    mid = book.mid
    if mid is None or mid <= 0.001 or mid >= 0.999:
        return None

    spread = book.spread or 0.01
    best_bid_p = book.best_bid or mid
    best_ask_p = book.best_ask or mid
    best_bid_sz = book.bids[0].size if book.bids else 0.0
    best_ask_sz = book.asks[0].size if book.asks else 0.0

    # Update rolling price history
    tid = book.token_id
    if tid not in _price_history:
        _price_history[tid] = deque(maxlen=_HISTORY_LEN)
    _price_history[tid].append(mid)

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
    try:
        from core.fundamental_ingest import fundamental_engine
        fund_sentiment = fundamental_engine.get_token_sentiment(book.token_id)
    except Exception:
        fund_sentiment = 0.0
    competitiveness = float(market.get("competitive") or 0.9)
    spread_compression = max(0.0, 1.0 - (spread / 0.05))
    whale_flow_index = np.clip(ofi * 0.8 + fund_sentiment * 0.2, -1.0, 1.0)

    # 8. Hurst Exponent via proper R/S analysis on extended rolling price history (60 bars)
    price_hist = list(_price_history.get(tid, []))
    hurst_exponent = _rs_hurst(price_hist)

    # 9. Price acceleration from rolling 3-bar difference
    if len(price_hist) >= 3:
        recent_drift = price_hist[-1] - price_hist[-3]
        price_accel = float(np.clip(recent_drift * 10.0, -1.0, 1.0))
    else:
        price_accel = float(micro_drift * 0.5)

    # 10. Slippage & depth slope
    slippage_est = min(spread / max(best_bid_sz + 1.0, 1.0) * 1000.0, 1.0)
    depth_slope = (cum_bid - best_bid_sz) / max(cum_bid + 1.0, 1.0)

    # 11. Cluster correlation — real cross-market computation
    cluster_corr = _cluster_correlation(tid, mid)

    # 12. Regime classification (one-hot, 4 flags — features 33-36)
    r_trending, r_mean_rev, r_volatile, r_resolution = _classify_regime(mid, spread, depth_imb_5lvl)

    # 13. Rolling price volatility (std of log-returns over last 10 bars — feature 37)
    if len(price_hist) >= 4:
        recent = price_hist[-min(11, len(price_hist)):]
        log_rets = np.diff(np.log(np.clip(recent, 1e-6, 1.0 - 1e-6)))
        rolling_vol = float(np.clip(np.std(log_rets) * 10.0, 0.0, 1.0))  # scale to [0,1]
    else:
        rolling_vol = float(spread_volatility * 0.5)

    # 14. Price momentum 5-bar (feature 38)
    if len(price_hist) >= 6:
        mom_5 = float(np.clip((price_hist[-1] - price_hist[-6]) * 10.0, -1.0, 1.0))
    else:
        price_accel_fallback = price_accel
        mom_5 = float(np.clip(price_accel_fallback * 0.5, -1.0, 1.0))

    vec = np.array([
        # ── Microstructure (1-18) ──────────────────────────────────────────────
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
        # ── Cyclical time (19-22) ──────────────────────────────────────────────
        float(hour_sin),
        float(hour_cos),
        float(day_sin),
        float(day_cos),
        # ── Market structure / fundamentals (23-32) ────────────────────────────
        float(competitiveness),
        float(spread_compression),
        float(fund_sentiment),
        float(whale_flow_index),
        float(hurst_exponent),
        float(price_accel),
        float(slippage_est),
        float(depth_slope),
        float(decay_accel),
        float(cluster_corr),
        # ── Regime one-hot flags (33-36) ──────────────────────────────────────
        float(r_trending),
        float(r_mean_rev),
        float(r_volatile),
        float(r_resolution),
        # ── Extended price dynamics (37-38) ───────────────────────────────────
        float(rolling_vol),
        float(mom_5),
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
