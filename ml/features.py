"""
ml/features.py — Feature engineering for the prediction market ML model.

Extracts a fixed-length feature vector from a Gamma market dict + live OrderBook.
All features are normalised to [0, 1] or [-1, 1] to work well with linear models.
"""
from __future__ import annotations

import datetime
import math
from typing import Optional

import numpy as np

from core.data_store import OrderBook


# Feature names — keep in sync with extract_features() output order
FEATURE_NAMES = [
    "mid_price",            # current mid price  [0,1]
    "spread_norm",          # spread / mid (relative spread)  [0,1]
    "bid_depth_norm",       # best bid size normalised  [0,1]
    "ask_depth_norm",       # best ask size normalised  [0,1]
    "vol_momentum",         # 24h vol / weekly avg  [0,2] clipped
    "vol_log",              # log10(volume24h + 1) / 7  [0,1]
    "days_left_norm",       # days until expiry  [0,1]
    "urgency",              # 1 / (days_left + 1)  approaching resolution
    "price_extremity",      # distance from 0.5, signed  [-1,1]
    "price_high",           # mid > 0.80  binary
    "price_low",            # mid < 0.20  binary
    "hour_sin",             # time-of-day sine  [-1,1]
    "hour_cos",             # time-of-day cosine  [-1,1]
]

N_FEATURES = len(FEATURE_NAMES)


def extract_features(market: dict, book: OrderBook) -> Optional[np.ndarray]:
    """
    Return a float32 feature vector of length N_FEATURES, or None if data is
    insufficient to compute features reliably.
    """
    mid = book.mid
    if mid is None or mid <= 0 or mid >= 1:
        return None

    spread = book.spread or 0.0
    best_bid_sz = book.bids[0].size if book.bids else 0.0
    best_ask_sz = book.asks[0].size if book.asks else 0.0

    # Volume features
    vol_24h = float(market.get("volume24hr") or 0)
    vol_total = float(market.get("volume") or 0)
    weekly_avg = vol_total / 7.0 if vol_total > 0 else 1.0
    vol_momentum = min(vol_24h / max(weekly_avg, 1.0), 3.0) / 3.0  # normalise to [0,1]
    vol_log = min(math.log10(vol_24h + 1) / 7.0, 1.0)

    # Time-to-expiry features
    days_left = _days_to_expiry(market)
    days_left_norm = min(days_left / 365.0, 1.0)
    urgency = 1.0 / (days_left + 1.0)

    # Price features
    price_extremity = (mid - 0.5) * 2.0          # in [-1, 1]
    price_high = 1.0 if mid > 0.80 else 0.0
    price_low  = 1.0 if mid < 0.20 else 0.0

    # Depth normalisation (cap at 10_000 shares)
    bid_depth = min(best_bid_sz / 10_000.0, 1.0)
    ask_depth = min(best_ask_sz / 10_000.0, 1.0)

    # Time-of-day (UTC)
    now = datetime.datetime.utcnow()
    hour_frac = (now.hour + now.minute / 60.0) / 24.0
    hour_sin = math.sin(2 * math.pi * hour_frac)
    hour_cos = math.cos(2 * math.pi * hour_frac)

    vec = np.array([
        float(mid),
        min(spread / max(mid, 0.01), 1.0),
        bid_depth,
        ask_depth,
        vol_momentum,
        vol_log,
        days_left_norm,
        min(urgency, 1.0),
        price_extremity,
        price_high,
        price_low,
        hour_sin,
        hour_cos,
    ], dtype=np.float32)

    # Safety: replace any NaN/Inf with 0
    vec = np.nan_to_num(vec, nan=0.0, posinf=1.0, neginf=-1.0)
    return vec


def _days_to_expiry(market: dict) -> float:
    """Return days until market resolution, default 30 if unknown."""
    for key in ("endDate", "end_date_iso", "endDateIso"):
        raw = market.get(key)
        if raw:
            try:
                if isinstance(raw, str):
                    raw = raw.replace("Z", "+00:00")
                    end = datetime.datetime.fromisoformat(raw)
                    end = end.replace(tzinfo=datetime.timezone.utc)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    return max((end - now).total_seconds() / 86400.0, 0.0)
            except Exception:
                continue
    return 30.0  # safe default
