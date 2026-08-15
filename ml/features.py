"""
ml/features.py — Quantitative Feature Engineering for Prediction Markets.

Extracts fixed-length feature vectors from market metadata and live OrderBook:
  1. Mid Price
  2. Relative Spread
  3. Order Flow Imbalance (OFI): (BidDepth - AskDepth) / (BidDepth + AskDepth)
  4. Micro-Price Drift: (MicroPrice - Mid)
  5. Best Bid Depth (Normalized)
  6. Best Ask Depth (Normalized)
  7. 24h Volume Momentum
  8. Log10 Volume
  9. Days Left to Expiry
 10. Resolution Urgency (1 / (days + 1))
 11. Price Extremity (|Mid - 0.5| * 2)
 12. Time of Day Sin
 13. Time of Day Cos
"""
from __future__ import annotations

import datetime
import math
from typing import Optional

import numpy as np

from core.data_store import OrderBook

FEATURE_NAMES = [
    "mid_price",
    "spread_norm",
    "order_flow_imbalance",
    "micro_price_drift",
    "bid_depth_norm",
    "ask_depth_norm",
    "vol_momentum",
    "vol_log",
    "days_left_norm",
    "urgency",
    "price_extremity",
    "hour_sin",
    "hour_cos",
]

N_FEATURES = len(FEATURE_NAMES)


def extract_features(market: dict, book: OrderBook) -> Optional[np.ndarray]:
    """
    Return float32 feature vector of length N_FEATURES, or None if insufficient book depth.
    """
    mid = book.mid
    if mid is None or mid <= 0 or mid >= 1:
        return None

    spread = book.spread or 0.0
    best_bid_p = book.best_bid or mid
    best_ask_p = book.best_ask or mid
    best_bid_sz = book.bids[0].size if book.bids else 0.0
    best_ask_sz = book.asks[0].size if book.asks else 0.0

    # 1. Order Flow Imbalance (OFI) ∈ [-1, 1]
    total_depth = best_bid_sz + best_ask_sz
    ofi = (best_bid_sz - best_ask_sz) / max(total_depth, 1.0)

    # 2. Micro-Price & Drift
    if total_depth > 0:
        micro_price = (best_bid_p * best_ask_sz + best_ask_p * best_bid_sz) / total_depth
    else:
        micro_price = mid
    micro_drift = np.clip((micro_price - mid) * 20.0, -1.0, 1.0)

    # 3. Volume Metrics
    vol_24h = float(market.get("volume24hr") or 0.0)
    vol_total = float(market.get("volume") or 0.0)
    weekly_avg = vol_total / 7.0 if vol_total > 0 else 1.0
    vol_momentum = min(vol_24h / max(weekly_avg, 1.0), 3.0) / 3.0
    vol_log = min(math.log10(vol_24h + 1) / 7.0, 1.0)

    # 4. Time to Expiry
    days_left = _days_to_expiry(market)
    days_left_norm = min(days_left / 365.0, 1.0)
    urgency = 1.0 / (days_left + 1.0)

    # 5. Price Extremity
    price_extremity = abs(mid - 0.5) * 2.0

    # 6. Depth Normalization
    bid_depth_norm = min(best_bid_sz / 5_000.0, 1.0)
    ask_depth_norm = min(best_ask_sz / 5_000.0, 1.0)

    # 7. Time of Day Cycle
    now = datetime.datetime.now(datetime.timezone.utc)
    hour_frac = (now.hour + now.minute / 60.0) / 24.0
    hour_sin = math.sin(2 * math.pi * hour_frac)
    hour_cos = math.cos(2 * math.pi * hour_frac)

    vec = np.array([
        float(mid),
        min(spread / max(mid, 0.01), 1.0),
        float(ofi),
        float(micro_drift),
        float(bid_depth_norm),
        float(ask_depth_norm),
        float(vol_momentum),
        float(vol_log),
        float(days_left_norm),
        float(min(urgency, 1.0)),
        float(price_extremity),
        float(hour_sin),
        float(hour_cos),
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
