"""Feature generation pipeline — transforms raw market data into ML features.

Takes validated data from the ingestion pipeline and generates:
1. Price-based features (returns, volatility, momentum)
2. Order book features (spread, depth, imbalance)
3. Volume features (VWAP, volume profile)
4. Temporal features (time to resolution, day of week)
5. Cross-market features (correlated market prices)

Each feature:
- Has a versioned definition (see ``ingestion.feature_contracts``)
- Is point-in-time correct (no look-ahead) — only snapshots with
  ``timestamp <= as_of`` are used; cyclical time features are derived
  from ``as_of`` rather than the wall clock; the rolling price-history
  deque is primed from historical snapshots, NOT from the live cache.
- Is stored with provenance (which raw snapshot was used + how many
  historical snapshots contributed to the rolling features). The
  provenance is persisted to the feature store alongside the feature
  values so an operator can audit any prediction's inputs.
- Is queryable by timestamp for backtesting —
  ``get_features(token_id, timestamp=T)`` reconstructs the feature
  vector the model would have seen at wall-clock time ``T``.

Wiring
------
The ML model never calls ``ml.features.extract_features`` directly any
more — it goes through this pipeline so every prediction's inputs
carry provenance + point-in-time guarantees::

    from ingestion.feature_pipeline import get_feature_pipeline
    pipe = get_feature_pipeline()
    features = await pipe.get_features(token_id, timestamp=time.time())
    p_yes = ml_model.predict_proba(features, token_id=token_id)

For convenience, the pipeline also exposes a one-shot
``pipe.predict_proba(token_id, ...)`` that fetches features and calls
``ml_model.predict_proba`` in one go.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import numpy as np

from core.data_store import OrderBook, PriceLevel
from ingestion.feature_contracts import (
    FEATURE_CONTRACTS,
    non_pit_feature_names,
    pit_feature_names,
    register_all_contracts,
)
from ml.features import FEATURE_NAMES, N_FEATURES, extract_features

log = logging.getLogger(__name__)


# ── Pluggable snapshot source ──────────────────────────────────────────────


class SnapshotSource(Protocol):
    """Pluggable source of historical market snapshots.

    The default implementation routes to
    ``core.database_manager.db_manager`` (which itself routes PG →
    SQLite fallback). Tests inject a simple in-memory list to make
    assertions deterministic without standing up a real DB.
    """

    async def get_snapshots(
        self, token_id: str, limit: int = ...
    ) -> list[dict[str, Any]]:
        ...


# ── Provenance ──────────────────────────────────────────────────────────────


@dataclass
class FeatureProvenance:
    """Provenance record for a single feature-vector computation.

    Captured at ``get_features`` / ``get_features_with_provenance`` time
    and persisted alongside the feature values in the feature store
    (via ``record_values(token_id, features, prediction_id=...)`` — the
    ``prediction_id`` field carries the provenance id so the
    ``feature_values`` rows for this prediction can be filtered back
    out by the audit / drift dashboards).
    """

    token_id: str
    as_of: float
    snapshot_timestamp: Optional[float]
    snapshot_source: Optional[str]
    history_points_used: int
    feature_version: str
    point_in_time_features: list[str] = field(default_factory=list)
    non_pit_features: list[str] = field(default_factory=list)
    provenance_id: str = ""

    def __post_init__(self) -> None:
        if not self.provenance_id:
            self.provenance_id = (
                f"pipeline-{self.feature_version}-{self.token_id[:8]}-"
                f"{int(self.as_of * 1000)}"
            )

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "as_of": self.as_of,
            "snapshot_timestamp": self.snapshot_timestamp,
            "snapshot_source": self.snapshot_source,
            "history_points_used": self.history_points_used,
            "feature_version": self.feature_version,
            "point_in_time_features": list(self.point_in_time_features),
            "non_pit_features": list(self.non_pit_features),
            "provenance_id": self.provenance_id,
        }


# ── Pipeline ────────────────────────────────────────────────────────────────


class FeaturePipeline:
    """Orchestrates point-in-time feature generation from validated market data.

    The pipeline is the **only** entry point the ML model should use to
    obtain features for prediction. It guarantees:

    1. **Provenance** — every feature vector returned carries a
       :class:`FeatureProvenance` record identifying which raw snapshot
       was used and how many historical snapshots contributed to the
       rolling features. The provenance is persisted to the feature
       store alongside the feature values via ``record_values``.
    2. **Point-in-time correctness** — only snapshots with
       ``timestamp <= as_of`` are consulted. Cyclical time features
       (``hour_sin`` / ``hour_cos`` / ``day_sin`` / ``day_cos``) are
       derived from ``as_of`` rather than the wall clock, so a
       backtest replaying T0 produces the *exact* feature vector the
       model would have seen at T0.
    3. **Versioning** — every contract in ``FEATURE_CONTRACTS`` carries
       a schema ``version`` (default ``v1``). A future re-definition
       bumps the version, and the new contract is registered alongside
       the old one in the feature store so historical rows remain
       addressable by their original version.

    Construction
    ------------
    ``db`` / ``feature_store`` / ``ml_model`` are all optional — defaults
    resolve lazily to the process-wide singletons. Tests inject fresh
    instances scoped to a ``tmp_path`` SQLite file (or an in-memory
    list of snapshot dicts via :class:`MockSnapshotSource`-style fakes)
    for hermetic isolation.
    """

    VERSION = "v1.0.0"

    # Indices in the 38-dim feature vector for the cyclical time features.
    # These are derived from ``as_of`` (NOT from ``datetime.now()``) so a
    # backtest replay at T0 produces the exact feature vector the model
    # would have seen at T0.
    _TIME_INDICES = {
        "hour_sin": FEATURE_NAMES.index("hour_sin"),
        "hour_cos": FEATURE_NAMES.index("hour_cos"),
        "day_sin": FEATURE_NAMES.index("day_sin"),
        "day_cos": FEATURE_NAMES.index("day_cos"),
    }

    def __init__(
        self,
        db: Optional[SnapshotSource] = None,
        feature_store: Optional[Any] = None,
        ml_model: Optional[Any] = None,
        history_window: int = 60,
    ) -> None:
        # Lazy-resolve the default singletons to avoid import-time side
        # effects (db_manager instantiates SQLite files; ml_model trains
        # on import). Tests pass their own instances for hermeticity.
        self._db = db
        self._feature_store = feature_store
        self._ml_model = ml_model
        self._history_window = max(history_window, 1)

        # Register the full catalog once at construction (idempotent upsert).
        # Deferred until the feature_store is actually accessed so a
        # pipeline constructed with ``feature_store=None`` (e.g. a unit
        # test that doesn't care about persistence) doesn't crash.
        self._contracts_registered = False

    # ── Lazy singleton resolution ────────────────────────────────────────

    def _resolve_db(self) -> SnapshotSource:
        if self._db is None:
            from core.database_manager import db_manager as _db
            self._db = _db
        return self._db  # type: ignore[return-value]

    def _resolve_feature_store(self) -> Any:
        if self._feature_store is None:
            from ml.feature_store import feature_store as _fs
            self._feature_store = _fs
        if not self._contracts_registered:
            try:
                register_all_contracts(self._feature_store, version=self.VERSION)
            except Exception:  # noqa: BLE001 — defensive
                log.debug(
                    "[feature_pipeline] contract registration skipped",
                    exc_info=True,
                )
            self._contracts_registered = True
        return self._feature_store

    def _resolve_ml_model(self, override: Optional[Any] = None) -> Any:
        if override is not None:
            return override
        if self._ml_model is not None:
            return self._ml_model
        # Lazy import — avoids the heavy ``MarketMLModel.load_or_create``
        # call at module import time so the pipeline can be constructed
        # in environments where the model isn't yet trained.
        from ml.model import ml_model as _default
        self._ml_model = _default
        return _default

    # ── Public API ──────────────────────────────────────────────────────

    async def get_features(
        self,
        token_id: str,
        timestamp: Optional[float] = None,
        market: Optional[dict] = None,
    ) -> Optional[np.ndarray]:
        """Return the 38-dim feature vector at ``timestamp`` (point-in-time).

        ``timestamp`` defaults to ``time.time()`` (live serving mode).
        Pass an explicit epoch float for backtest replay.

        Returns ``None`` when no snapshot is available at-or-before
        ``timestamp`` (the model cannot score a token it has no data for).
        """
        vec, _ = await self.get_features_with_provenance(
            token_id, timestamp=timestamp, market=market
        )
        return vec

    async def get_features_with_provenance(
        self,
        token_id: str,
        timestamp: Optional[float] = None,
        market: Optional[dict] = None,
    ) -> tuple[Optional[np.ndarray], Optional[FeatureProvenance]]:
        """Compute the feature vector + provenance record at ``timestamp``.

        The pipeline:
          1. Pulls the recent snapshot window for ``token_id`` (default
             ``history_window + 5`` rows, most-recent-first).
          2. Filters to those with ``observation timestamp <= as_of``.
          3. Picks the latest as the snapshot to score; the rest prime
             the rolling price-history deque (so Hurst / momentum /
             rolling-vol features are NOT contaminated by future data).
          4. Reconstructs an :class:`OrderBook` from the snapshot's
             ``bids_json`` / ``asks_json`` ladder (falling back to the
             top-of-book ``best_bid`` / ``best_ask`` columns when the
             ladder isn't present).
          5. Calls :func:`ml.features.extract_features` with the
             reconstructed book + market dict (the canonical 38-dim
             feature extractor — the pipeline does NOT reimplement it).
          6. Overwrites the four cyclical time features with
             point-in-time values derived from ``as_of``.
          7. Persists the feature values to the feature store with the
             provenance record's ``provenance_id`` as the
             ``prediction_id`` foreign key.

        Returns ``(None, None)`` if no point-in-time-correct snapshot
        could be reconstructed (the token has no rows in the snapshot
        source at or before ``as_of``).
        """
        as_of = float(timestamp) if timestamp is not None else time.time()

        # 1. Fetch the recent snapshot window and filter to those <= as_of.
        rows = await self._fetch_snapshot_window(
            token_id, limit=self._history_window + 5
        )
        pit_rows = [
            r for r in rows
            if self._row_ts(r) is not None and self._row_ts(r) <= as_of
        ]
        if not pit_rows:
            return None, None

        # 2. Order ascending by observation timestamp. The LAST entry
        # is the snapshot to score; the rest prime the price history.
        pit_rows.sort(key=lambda r: self._row_ts(r) or 0.0)  # type: ignore[arg-type]
        snapshot_row = pit_rows[-1]
        history_rows = pit_rows[:-1]  # strictly older than as_of

        # 3. Reconstruct the OrderBook from the snapshot's JSON ladder.
        book = self._reconstruct_order_book(token_id, snapshot_row)
        if book is None or book.mid is None:
            return None, None

        # 4. Prime the rolling price-history deque so Hurst / momentum /
        # rolling-vol features use ONLY data <= as_of. The deque is
        # cleared first so a prior call's state cannot leak in.
        self._prime_price_history(token_id, history_rows)

        # 5. Build the market dict from the snapshot row (the caller's
        # explicit ``market=`` takes precedence; fields not supplied
        # by the caller fall back to the snapshot row's columns).
        market_dict: dict[str, Any] = dict(market or {})
        self._merge_market_fields(market_dict, snapshot_row)

        # 6. Extract features via the canonical extractor.
        vec = extract_features(market_dict, book)
        if vec is None:
            return None, None

        # 7. Override cyclical time features with point-in-time values
        # derived from ``as_of`` rather than the wall clock.
        vec = self._override_time_features(vec, as_of)

        # 8. Build + persist provenance.
        provenance = FeatureProvenance(
            token_id=token_id,
            as_of=as_of,
            snapshot_timestamp=self._row_ts(snapshot_row),
            snapshot_source=(
                snapshot_row.get("source")
                or snapshot_row.get("slug")
                or "unknown"
            ),
            history_points_used=len(history_rows),
            feature_version=self.VERSION,
            point_in_time_features=pit_feature_names(),
            non_pit_features=non_pit_feature_names(),
        )
        self._persist(token_id, vec, provenance)
        return vec, provenance

    async def get_feature_age(
        self,
        token_id: str,
        as_of: Optional[float] = None,
    ) -> Optional[float]:
        """Return the age in seconds of the most recent snapshot for ``token_id``.

        W33-2 — used by :meth:`ml.model.MarketMLModel.predict_proba` for the
        feature-freshness check. The ML model is permitted (with a logged
        warning) to score against stale features when the most-recent
        snapshot is older than the freshness threshold (default 60 s), but
        a sub-second age is the healthy steady state.

        ``as_of`` defaults to ``time.time()`` (live serving). Pass an
        explicit epoch float for backtest replay — the age is then measured
        against ``as_of``, NOT the wall clock, so a replay at T0 sees the
        freshness the model would have seen at T0.

        Returns ``None`` when the token has no snapshot at-or-before
        ``as_of`` (the model should treat this identically to "no
        features available" — i.e. fall back to the neutral 0.5 prediction
        via :meth:`MarketMLModel.predict_proba`'s fallback path).
        """
        as_of = float(as_of) if as_of is not None else time.time()
        rows = await self._fetch_snapshot_window(token_id, limit=1)
        if not rows:
            return None
        ts = self._row_ts(rows[0])
        if ts is None:
            return None
        # Age is non-negative — a snapshot with ts > as_of (a future
        # snapshot leaking past the PIT window) is treated as 0.0 so
        # the freshness check doesn't surface a nonsensical negative age.
        return max(0.0, as_of - ts)

    async def predict(
        self,
        token_id: str,
        ml_model: Optional[Any] = None,
        timestamp: Optional[float] = None,
        market: Optional[dict] = None,
    ) -> Optional[tuple[float, float]]:
        """Wire features → ``ml_model.predict`` (point-in-time when ``timestamp`` is set).

        Returns ``None`` if no feature vector could be produced (the
        caller should treat this as "no prediction possible" — e.g.
        the token has no validated snapshot at-or-before ``timestamp``).

        Otherwise returns the ``(p_yes, confidence)`` tuple from the ML
        model. The default ``ml_model`` is the process-wide singleton
        (``ml.model.ml_model``); pass an explicit ``ml_model=`` to
        override (e.g. for tests or for shadow inference).
        """
        features = await self.get_features(
            token_id, timestamp=timestamp, market=market
        )
        if features is None:
            return None
        model = self._resolve_ml_model(ml_model)
        p_yes, confidence = model.predict(features, token_id=token_id)
        return float(p_yes), float(confidence)

    async def predict_proba(
        self,
        token_id: str,
        ml_model: Optional[Any] = None,
        timestamp: Optional[float] = None,
        market: Optional[dict] = None,
    ) -> Optional[float]:
        """Convenience wrapper returning only ``p_yes``.

        Mirrors ``ml_model.predict_proba(features, token_id=token_id)`` —
        the single-number API most call sites want (the full
        ``(p_yes, confidence)`` tuple is available via :meth:`predict`).
        """
        result = await self.predict(
            token_id, ml_model=ml_model, timestamp=timestamp, market=market
        )
        return None if result is None else result[0]

    def invalidate(self, token_id: str) -> bool:
        """Invalidate cached state for ``token_id``.

        W33-4 — called by ``ingestion.market_events.MarketEventIngester``
        on ``MARKET_RESOLVED`` so the next prediction uses fresh data
        rather than the resolved market's stale price history (a
        resolved market's mid converges to 0.0 or 1.0, which would
        contaminate the Hurst / momentum / rolling-vol estimators on
        the next call).

        Clears the per-token rolling price-history deque inside
        ``ml.features._price_history`` (the only state the pipeline
        itself caches — the snapshot window is fetched fresh from the
        snapshot source on every ``get_features`` call, so there's no
        other cache to invalidate).

        Args:
            token_id: The Polymarket market token id whose cached
                state should be cleared.

        Returns:
            ``True`` when a cached entry was found and cleared;
            ``False`` when the token had no cached entry (no-op).
        """
        try:
            from ml import features as _feat
        except Exception as e:  # noqa: BLE001 — defensive
            log.debug(
                "[feature_pipeline] invalidate: ml.features unavailable: %s",
                e,
            )
            return False
        history = _feat._price_history
        if token_id in history:
            history.pop(token_id, None)
            log.debug(
                "[feature_pipeline] invalidated cached price history for %s",
                token_id,
            )
            return True
        return False

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _fetch_snapshot_window(
        self, token_id: str, limit: int
    ) -> list[dict]:
        """Fetch the most-recent-``limit`` snapshot rows for ``token_id``.

        Delegates to the snapshot source (default: ``db_manager``).
        Returns an empty list on any error so the caller can fall back
        to the "no data" path gracefully.
        """
        try:
            db = self._resolve_db()
            return await db.get_snapshots(token_id, limit=limit)
        except Exception as e:  # noqa: BLE001 — defensive
            log.debug("[feature_pipeline] snapshot fetch failed: %s", e)
            return []

    @staticmethod
    def _row_ts(row: dict) -> Optional[float]:
        """Extract the observation timestamp from a snapshot row.

        Prefers ``timestamp`` (epoch when the observation was made);
        falls back to ``ingestion_time`` (when it was stored). Returns
        ``None`` if neither field is parseable — the caller filters
        such rows out of the point-in-time window.
        """
        for key in ("timestamp", "ingestion_time"):
            ts = row.get(key)
            if ts is None:
                continue
            try:
                return float(ts)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _reconstruct_order_book(
        token_id: str, row: dict
    ) -> Optional[OrderBook]:
        """Build an :class:`OrderBook` from a snapshot row.

        Parses the JSON-encoded ``bids_json`` / ``asks_json`` ladders
        into ``PriceLevel`` lists. Falls back to a single-level
        top-of-book (from ``best_bid`` / ``best_ask``) when the ladder
        columns are absent — the live snapshot always has them, but a
        legacy or hand-crafted test row might not.

        Returns ``None`` if neither the ladder nor the top-of-book
        columns produce at least one bid AND one ask (the book must be
        two-sided for ``extract_features`` to compute a ``mid``).
        """
        bids = _parse_levels(row.get("bids_json") or row.get("bids"))
        asks = _parse_levels(row.get("asks_json") or row.get("asks"))

        if not bids and row.get("best_bid") is not None:
            try:
                bids = [PriceLevel(
                    price=float(row["best_bid"]),
                    size=float(row.get("bid_size") or 0.0),
                )]
            except (TypeError, ValueError):
                bids = []
        if not asks and row.get("best_ask") is not None:
            try:
                asks = [PriceLevel(
                    price=float(row["best_ask"]),
                    size=float(row.get("ask_size") or 0.0),
                )]
            except (TypeError, ValueError):
                asks = []

        if not bids or not asks:
            return None

        ts = FeaturePipeline._row_ts(row) or time.time()
        return OrderBook(
            token_id=token_id, bids=bids, asks=asks, updated_at=ts
        )

    @staticmethod
    def _prime_price_history(
        token_id: str, history_rows: list[dict]
    ) -> None:
        """Pre-populate ``ml.features._price_history`` with PIT-correct mids.

        The deque is cleared first so a prior call's state cannot leak
        in. ``history_rows`` is expected to be ASCENDING by observation
        timestamp (the caller sorts). Rows whose mid is missing or
        outside the tradeable (0, 1) band are skipped — they would
        corrupt the Hurst / rolling-vol estimators.
        """
        # Local import — avoids importing ``ml.features`` at module
        # load time (which itself imports ``core.data_store`` and
        # ``core.fundamental_ingest``).
        from ml import features as _feat

        history = _feat._price_history.setdefault(
            token_id, deque(maxlen=_feat._HISTORY_LEN)
        )
        history.clear()
        for row in sorted(
            history_rows, key=lambda r: FeaturePipeline._row_ts(r) or 0.0
        ):
            mid = row.get("mid")
            if mid is None:
                bb = row.get("best_bid")
                ba = row.get("best_ask")
                if bb is not None and ba is not None:
                    try:
                        mid = (float(bb) + float(ba)) / 2.0
                    except (TypeError, ValueError):
                        mid = None
            if mid is None:
                continue
            try:
                mid_f = float(mid)
            except (TypeError, ValueError):
                continue
            if not (0.0 < mid_f < 1.0):
                continue
            history.append(mid_f)

    @staticmethod
    def _override_time_features(vec: np.ndarray, as_of: float) -> np.ndarray:
        """Overwrite cyclical time features with values derived from ``as_of``.

        ``extract_features`` derives these from ``datetime.now()`` which
        is correct for live serving but wrong for backtesting (a replay
        at T0 would otherwise see the wall-clock time of the replay
        run, not T0). This post-processing step replaces those four
        entries with the ``as_of``-derived values so the feature vector
        is byte-identical to what the model would have seen at T0.
        """
        dt = datetime.datetime.fromtimestamp(as_of, tz=datetime.timezone.utc)
        hour_frac = (dt.hour + dt.minute / 60.0 + dt.second / 3600.0) / 24.0
        day_frac = dt.weekday() / 7.0
        idx = FeaturePipeline._TIME_INDICES
        vec[idx["hour_sin"]] = float(math.sin(2 * math.pi * hour_frac))
        vec[idx["hour_cos"]] = float(math.cos(2 * math.pi * hour_frac))
        vec[idx["day_sin"]] = float(math.sin(2 * math.pi * day_frac))
        vec[idx["day_cos"]] = float(math.cos(2 * math.pi * day_frac))
        return vec

    @staticmethod
    def _merge_market_fields(market_dict: dict, row: dict) -> None:
        """Pull static market fields from the snapshot row when the caller
        didn't supply them. The snapshot row carries the volume /
        liquidity / end-date fields the ``extract_features`` formula
        needs; the caller's explicit ``market=`` always wins.
        """
        for key in (
            "volume24hr", "volume_24h", "volume", "liquidity",
            "liquidityNum", "endDate", "end_date_iso", "endDateIso",
        ):
            if key not in market_dict and row.get(key) is not None:
                market_dict[key] = row[key]

    def _persist(
        self, token_id: str, vec: np.ndarray, provenance: FeatureProvenance
    ) -> None:
        """Persist feature values + provenance to the feature store.

        Defensive try/except — a transient SQLite hiccup must NEVER
        degrade the prediction path (mirrors the same pattern in
        ``ml/model.py::predict`` for its own feature-store record).
        """
        try:
            store = self._resolve_feature_store()
            features = {
                FEATURE_NAMES[i]: float(vec[i]) for i in range(len(FEATURE_NAMES))
            }
            store.record_values(
                token_id=token_id,
                features=features,
                prediction_id=provenance.provenance_id,
            )
        except Exception:  # noqa: BLE001 — defensive
            log.debug(
                "[feature_pipeline] feature-store record skipped",
                exc_info=True,
            )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_levels(raw: Any) -> list[PriceLevel]:
    """Parse a JSON-encoded ladder (or a list of dicts) into ``PriceLevel``s.

    Accepts both shapes the snapshot stores use:
    * ``"[{\"price\": 0.49, \"size\": 100}, ...]"`` (JSON string — the
      ``market_snapshots.bids_json`` column shape).
    * ``[{"price": 0.49, "size": 100}, ...]`` (already-decoded list —
      the in-memory ``MockSnapshotSource`` test shape).

    Returns an empty list on any parse error so the caller can fall
    back to the top-of-book columns.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    levels: list[PriceLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            levels.append(PriceLevel(
                price=float(item["price"]),
                size=float(item["size"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return levels


# ── Module-level singleton ──────────────────────────────────────────────────


_feature_pipeline: Optional[FeaturePipeline] = None


def get_feature_pipeline() -> FeaturePipeline:
    """Return the process-wide :class:`FeaturePipeline` singleton.

    Constructed lazily on first access so module import is cheap and
    side-effect free. The singleton is wired to the default
    ``db_manager`` + ``feature_store`` singletons. The ``ml_model`` is
    resolved lazily inside :meth:`FeaturePipeline.predict` so a test
    that only exercises :meth:`get_features` doesn't trigger the
    heavy ``MarketMLModel.load_or_create`` call.
    """
    global _feature_pipeline
    if _feature_pipeline is None:
        _feature_pipeline = FeaturePipeline()
    return _feature_pipeline


__all__ = [
    "FeaturePipeline",
    "FeatureProvenance",
    "SnapshotSource",
    "get_feature_pipeline",
]
