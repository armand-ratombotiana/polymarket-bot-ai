"""Feature contracts — explicit schemas for each feature.

Each feature definition includes:
- name: Unique identifier
- type: "numeric", "categorical", "boolean"
- source: Which data domain it comes from
- formula: How it's computed
- window: Time window (if applicable)
- frequency: How often it's computed
- version: Schema version
- point_in_time: Whether it's safe for backtesting

W31-6 — The ML model is trained against a fixed 38-feature vector
defined in ``ml/features.py::FEATURE_NAMES``. This module is the
explicit, versioned contract for every one of those 38 features so
that:

  * **Provenance is addressable.** A prediction in the feature store
    can be traced back to the exact formula + window + version that
    produced each feature value, not just the feature's name.

  * **Point-in-time correctness is auditable.** Every contract
    declares whether the feature is safe to use in a backtest replay
    (``point_in_time=True``) or depends on live / future data
    (``point_in_time=False``). The :class:`FeaturePipeline` carries
    this list in its provenance record so a downstream consumer can
    filter out non-PIT features when replaying.

  * **Schema evolution is explicit.** Bumping a feature's formula
    bumps its ``version`` and the contract becomes a new entry — the
    historical rows remain queryable under the old version.

The 38 contracts below are organised by the same five domains the
task spec calls out (price-based / order book / volume / temporal /
cross-market), with one extra ``derived`` bucket for features
computed from other features.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The ML model's canonical 38-feature catalog. Importing here (rather
# than re-typing the names) guarantees the contract list is in lock-step
# with ``ml/features.py`` — a feature added there but missing here is
# caught at import time (``IndexError`` on the ``assert`` below).
from ml.features import FEATURE_NAMES

# ── Source-domain tags ─────────────────────────────────────────────────────
# Single source of truth so contract authors cannot invent new domains
# ad hoc. ``derived`` covers features computed from other features
# (e.g. ``price_extremity`` is derived from ``mid_price``).
SOURCE_ORDER_BOOK = "order_book"
SOURCE_PRICE_BASED = "price_based"
SOURCE_VOLUME = "volume"
SOURCE_TEMPORAL = "temporal"
SOURCE_CROSS_MARKET = "cross_market"
SOURCE_PRICE_HISTORY = "price_history"
SOURCE_FUNDAMENTAL = "fundamental"
SOURCE_DERIVED = "derived"

# Frequency constants — single vocabulary.
FREQ_PER_SNAPSHOT = "per-snapshot"
FREQ_HOURLY = "hourly"
FREQ_DAILY = "daily"

# Type constants.
TYPE_NUMERIC = "numeric"
TYPE_CATEGORICAL = "categorical"
TYPE_BOOLEAN = "boolean"


@dataclass(frozen=True)
class FeatureContract:
    """Explicit, versioned schema for a single ML feature.

    A contract is the source of truth for:
    * what a feature *means* (formula + source domain),
    * how often it should be recomputed (frequency),
    * whether it's safe to use in a point-in-time / backtest context
      (``point_in_time=True`` means no look-ahead bias),
    * which schema version the contract is at (so a future re-definition
      can bump ``version`` without rewriting historical rows).

    The contract is **frozen** so it cannot be mutated after
    registration — a feature's definition is immutable for the
    lifetime of a ``FeaturePipeline`` instance. Schema evolution
    happens by bumping ``version`` and registering a NEW contract
    (the old version remains queryable in the feature store's
    ``feature_definitions`` table).
    """

    name: str
    type: str
    source: str
    formula: str
    window: Optional[str] = None
    frequency: str = FREQ_PER_SNAPSHOT
    version: str = "v1"
    point_in_time: bool = True

    def description(self) -> str:
        """Render a compact human-readable description for the feature store.

        The ``feature_definitions.description`` column is a single TEXT
        field; this renders every contract field into that column so an
        operator pulling ``GET /api/features`` sees the full provenance
        without a second join.
        """
        return (
            f"[{self.source}/{self.version}] {self.formula}"
            f" (window={self.window or '-'}, freq={self.frequency},"
            f" pit={'yes' if self.point_in_time else 'no'})"
        )


# ── The 38 contracts ───────────────────────────────────────────────────────
# Order MUST match ``ml.features.FEATURE_NAMES`` so a contract can be
# looked up by index OR by name. The ``assert`` below catches a
# contract / catalog drift at import time.
FEATURE_CONTRACTS: dict[str, FeatureContract] = {
    # ── Price-based (1) ────────────────────────────────────────────────────
    "mid_price": FeatureContract(
        name="mid_price",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="(best_bid + best_ask) / 2",
    ),

    # ── Order book features (2-9, 17, 29, 30) ─────────────────────────────
    "spread_norm": FeatureContract(
        name="spread_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(spread / max(mid, 0.01), 1.0)",
    ),
    "order_flow_imbalance": FeatureContract(
        name="order_flow_imbalance",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="(best_bid_sz - best_ask_sz) / max(best_bid_sz + best_ask_sz, 1.0)",
    ),
    "micro_price_drift": FeatureContract(
        name="micro_price_drift",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="clip((micro_price - mid) * 20, -1, 1)",
    ),
    "bid_depth_norm": FeatureContract(
        name="bid_depth_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(best_bid_sz / 5000, 1.0)",
    ),
    "ask_depth_norm": FeatureContract(
        name="ask_depth_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(best_ask_sz / 5000, 1.0)",
    ),
    "cum_bid_depth_norm": FeatureContract(
        name="cum_bid_depth_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(sum(bid_sizes[:5]) / 25000, 1.0)",
        window="5-level",
    ),
    "cum_ask_depth_norm": FeatureContract(
        name="cum_ask_depth_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(sum(ask_sizes[:5]) / 25000, 1.0)",
        window="5-level",
    ),
    "depth_imbalance_ratio": FeatureContract(
        name="depth_imbalance_ratio",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="(cum_bid_5 - cum_ask_5) / max(cum_bid_5 + cum_ask_5, 1.0)",
        window="5-level",
    ),
    "spread_volatility": FeatureContract(
        name="spread_volatility",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(spread * 10, 1.0)",
    ),
    "slippage_estimate": FeatureContract(
        name="slippage_estimate",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="min(spread / max(best_bid_sz + 1, 1) * 1000, 1.0)",
    ),
    "depth_slope": FeatureContract(
        name="depth_slope",
        type=TYPE_NUMERIC,
        source=SOURCE_ORDER_BOOK,
        formula="(cum_bid - best_bid_sz) / max(cum_bid + 1, 1)",
        window="5-level",
    ),

    # ── Volume features (10-12) ────────────────────────────────────────────
    "vol_momentum": FeatureContract(
        name="vol_momentum",
        type=TYPE_NUMERIC,
        source=SOURCE_VOLUME,
        formula="min(vol_24h / max(vol_total / 7, 1), 3.0) / 3.0",
        window="24h",
    ),
    "vol_log": FeatureContract(
        name="vol_log",
        type=TYPE_NUMERIC,
        source=SOURCE_VOLUME,
        formula="min(log10(vol_24h + 1) / 7, 1.0)",
        window="24h",
    ),
    "liquidity_log": FeatureContract(
        name="liquidity_log",
        type=TYPE_NUMERIC,
        source=SOURCE_VOLUME,
        formula="min(log10(liquidity + 1) / 7, 1.0)",
    ),

    # ── Temporal features (13-14, 19-22, 31) ──────────────────────────────
    "days_left_norm": FeatureContract(
        name="days_left_norm",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="min(days_to_expiry / 365, 1.0)",
    ),
    "urgency": FeatureContract(
        name="urgency",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="min(1 / (days_left + 1), 1.0)",
    ),
    "hour_sin": FeatureContract(
        name="hour_sin",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="sin(2*pi*hour_fraction_utc)",
        frequency=FREQ_HOURLY,
    ),
    "hour_cos": FeatureContract(
        name="hour_cos",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="cos(2*pi*hour_fraction_utc)",
        frequency=FREQ_HOURLY,
    ),
    "day_sin": FeatureContract(
        name="day_sin",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="sin(2*pi*weekday/7)",
        frequency=FREQ_DAILY,
    ),
    "day_cos": FeatureContract(
        name="day_cos",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="cos(2*pi*weekday/7)",
        frequency=FREQ_DAILY,
    ),
    "decay_acceleration": FeatureContract(
        name="decay_acceleration",
        type=TYPE_NUMERIC,
        source=SOURCE_TEMPORAL,
        formula="min(1 / sqrt(days_left + 0.1) / 3, 1.0)",
    ),

    # ── Price-based derived (15, 16, 18, 23, 24, 26) ──────────────────────
    "price_extremity": FeatureContract(
        name="price_extremity",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="abs(mid - 0.5) * 2",
    ),
    "price_skewness": FeatureContract(
        name="price_skewness",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="(mid - 0.5) * 2",
    ),
    "binary_variance": FeatureContract(
        name="binary_variance",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="4 * mid * (1 - mid)",
    ),
    "competitiveness": FeatureContract(
        name="competitiveness",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="clip(1 - spread / 0.05, -1, 1)",
    ),
    "spread_compression": FeatureContract(
        name="spread_compression",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="max(0, 1 - spread / 0.05)",
    ),
    "whale_flow_index": FeatureContract(
        name="whale_flow_index",
        type=TYPE_NUMERIC,
        source=SOURCE_DERIVED,
        formula="clip(ofi * 0.8 + fundamental_sentiment * 0.2, -1, 1)",
    ),

    # ── Fundamental sentiment (25) ────────────────────────────────────────
    "fundamental_sentiment": FeatureContract(
        name="fundamental_sentiment",
        type=TYPE_NUMERIC,
        source=SOURCE_FUNDAMENTAL,
        # Sentiment is sourced from the live fundamental_ingest cache which
        # is itself fed by RSS / news polling. The value at backtest time T
        # is the LATEST sentiment the cache held at T, which may lag the
        # underlying news event by the poll interval. Marked partial PIT.
        formula="fundamental_engine.get_token_sentiment(token_id)",
        point_in_time=False,
    ),

    # ── Price-history rolling features (27, 28, 37, 38) ──────────────────
    "hurst_exponent": FeatureContract(
        name="hurst_exponent",
        type=TYPE_NUMERIC,
        source=SOURCE_PRICE_HISTORY,
        formula="log(R/S) / log(n) where R/S = rescaled_range(log_returns)",
        window="60-bar",
    ),
    "price_acceleration": FeatureContract(
        name="price_acceleration",
        type=TYPE_NUMERIC,
        source=SOURCE_PRICE_HISTORY,
        formula="clip((price_hist[-1] - price_hist[-3]) * 10, -1, 1)",
        window="3-bar",
    ),
    "rolling_volatility": FeatureContract(
        name="rolling_volatility",
        type=TYPE_NUMERIC,
        source=SOURCE_PRICE_HISTORY,
        formula="clip(std(log_returns[-10:]) * 10, 0, 1)",
        window="10-bar",
    ),
    "price_momentum_5bar": FeatureContract(
        name="price_momentum_5bar",
        type=TYPE_NUMERIC,
        source=SOURCE_PRICE_HISTORY,
        formula="clip((price_hist[-1] - price_hist[-6]) * 10, -1, 1)",
        window="5-bar",
    ),

    # ── Cross-market (32) ─────────────────────────────────────────────────
    "cluster_correlation": FeatureContract(
        name="cluster_correlation",
        type=TYPE_NUMERIC,
        source=SOURCE_CROSS_MARKET,
        # Cross-market correlation reads the LIVE ``store.order_books`` cache
        # to count markets whose mid is within ±0.05 of this token's mid.
        # A backtest replay can't reconstruct the live cache state at T
        # without a cross-token snapshot query, so the feature is flagged
        # NOT point-in-time. The FeaturePipeline records this in its
        # provenance so consumers can re-compute it if needed.
        formula="fraction of live order_books whose |mid - this_mid| <= 0.05",
        point_in_time=False,
    ),

    # ── Regime one-hot flags (33-36) ───────────────────────────────────────
    "regime_trending": FeatureContract(
        name="regime_trending",
        type=TYPE_BOOLEAN,
        source=SOURCE_DERIVED,
        formula="1 if abs(depth_imb_5lvl) > 0.40 else 0",
    ),
    "regime_mean_reverting": FeatureContract(
        name="regime_mean_reverting",
        type=TYPE_BOOLEAN,
        source=SOURCE_DERIVED,
        formula="1 if no other regime matches else 0",
    ),
    "regime_volatile": FeatureContract(
        name="regime_volatile",
        type=TYPE_BOOLEAN,
        source=SOURCE_DERIVED,
        formula="1 if spread >= 0.04 else 0",
    ),
    "regime_resolution": FeatureContract(
        name="regime_resolution",
        type=TYPE_BOOLEAN,
        source=SOURCE_DERIVED,
        formula="1 if mid >= 0.92 or mid <= 0.08 else 0",
    ),
}

# ── Sanity check: every name in FEATURE_NAMES has a contract and vice-versa.
assert set(FEATURE_CONTRACTS.keys()) == set(FEATURE_NAMES), (
    "Feature contract catalog is out of sync with ml.features.FEATURE_NAMES.\n"
    f"  Missing contracts: {set(FEATURE_NAMES) - set(FEATURE_CONTRACTS.keys())}\n"
    f"  Extra contracts:   {set(FEATURE_CONTRACTS.keys()) - set(FEATURE_NAMES)}\n"
    "Add or remove entries in FEATURE_CONTRACTS to match the ML model's catalog."
)


def register_all_contracts(feature_store: Any, version: str = "v1") -> int:
    """Register every contract in ``FEATURE_CONTRACTS`` into ``feature_store``.

    Idempotent — re-registration upserts. Returns the number of features
    registered. Defensive against ``feature_store=None`` (returns 0).

    Args:
        feature_store: a :class:`ml.feature_store.FeatureStore` (or any
            object exposing ``register_feature(name, type, description,
            min_value, max_value)``). ``None`` is treated as a no-op so
            the pipeline can be constructed in test contexts where the
            store isn't available.
        version: schema version stamp applied to each registered
            feature's description (for human-readable provenance).
    """
    if feature_store is None:
        return 0
    count = 0
    for contract in FEATURE_CONTRACTS.values():
        try:
            feature_store.register_feature(
                name=contract.name,
                type=contract.type,
                description=f"{contract.description()} [pipeline={version}]",
            )
            count += 1
        except Exception:  # noqa: BLE001 — defensive: a transient SQLite
            # hiccup must NEVER block pipeline construction. The
            # feature_store has its own retry-on-write semantics for the
            # per-prediction record_values path; the contract
            # registration is a one-time-upsert so a missed contract
            # is recoverable on the next pipeline construction.
            pass
    return count


def pit_feature_names() -> list[str]:
    """Names of every contract with ``point_in_time=True``."""
    return [c.name for c in FEATURE_CONTRACTS.values() if c.point_in_time]


def non_pit_feature_names() -> list[str]:
    """Names of every contract with ``point_in_time=False``."""
    return [c.name for c in FEATURE_CONTRACTS.values() if not c.point_in_time]


__all__ = [
    "FeatureContract",
    "FEATURE_CONTRACTS",
    "register_all_contracts",
    "pit_feature_names",
    "non_pit_feature_names",
    "SOURCE_ORDER_BOOK",
    "SOURCE_PRICE_BASED",
    "SOURCE_VOLUME",
    "SOURCE_TEMPORAL",
    "SOURCE_CROSS_MARKET",
    "SOURCE_PRICE_HISTORY",
    "SOURCE_FUNDAMENTAL",
    "SOURCE_DERIVED",
    "FREQ_PER_SNAPSHOT",
    "FREQ_HOURLY",
    "FREQ_DAILY",
    "TYPE_NUMERIC",
    "TYPE_CATEGORICAL",
    "TYPE_BOOLEAN",
]
