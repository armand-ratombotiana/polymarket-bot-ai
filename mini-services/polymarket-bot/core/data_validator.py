"""Data ingestion validator — ensures data quality at ingestion time.

W24-4 — Ingestion-time data quality gate. Sits in front of every
``record_snapshot`` / ``record_trade`` call site in the polymarket-bot
pipeline (``core/book_poller.py::_apply_book``,
``core/trade_ingester.py::_ingest_trades``) and either:

  * accepts the record (returns ``is_valid=True`` with a normalised
    payload augmented with provenance fields — ``ingestion_time`` /
    ``processing_time`` / ``source``), or
  * rejects it (returns ``is_valid=False``) with the rejection reason
    in ``errors`` (or ``is_duplicate=True`` for the dedup fast-path).

Six classes of check (mirroring the W24-4 task spec):

  1. **Deduplication** — skip records we've already seen (snapshots
     by a 4-field sha256 hash; trades by ``trade_id``). The
     in-memory dedup sets are bounded ``collections.deque``s of
     ``max_seen_ids`` entries (default 10k) so a long-running session
     can't grow them without limit — the durable UNIQUE constraint on
     ``trade_id`` (TimescaleDB ``market.market_trade`` hypertable +
     SQLite ``market_trades``) is the backstop for restarts / replays.
  2. **Timestamp normalisation** — every timestamp is coerced to a
     Unix-epoch ``float``. Accepts ``int`` / ``float`` / numeric
     strings / ISO-8601 strings. Missing timestamps fall back to
     ``ingestion_time`` and emit a warning so the operator can see the
     downstream effect (``best_bid`` / ``best_ask`` are still valid
     even if the upstream didn't set a timestamp).
  3. **Staleness detection** — if the (normalised) timestamp is more
     than 60s in the past, emit a warning; more than 300s, reject the
     record outright (``errors`` carries the message). The very-stale
     branch is checked FIRST so a 600s-old record is rejected, not
     just warned about.
  4. **Schema validation** — required fields per record type are
     checked (snapshots: ``token_id`` / ``best_bid`` / ``best_ask``;
     trades: ``token_id`` / ``price`` / ``size`` / ``side``). Missing
     fields are recorded in ``errors`` and the record is rejected.
  5. **Value validation** — prices must be in ``[0, 1]`` for
     prediction markets (negative prices → error; > 1.0 → warning
     since multi-outcome markets can technically quote ``> 1`` on a
     single token before normalisation); sizes must be ``> 0``; sides
     must be ``BUY`` or ``SELL``.
  6. **Provenance** — the normalised payload carries ``source`` (from
     ``raw_data.get("source", "unknown")``), ``ingestion_time``
     (``time.time()`` at validation start — captured ONCE so
     downstream consumers see a consistent value), and
     ``processing_time`` (``time.time()`` at validation end — close
     to ``ingestion_time`` but exposed separately so a profiler can
     measure validation overhead per record).

Singleton pattern
-----------------
A module-level ``data_validator`` singleton mirrors the convention
used by every sibling background-task module
(``core/book_poller.book_poller``, ``core/data_quality.data_quality_monitor``,
``core/clob_client.clob_client`` …). Importers grab it at module-import
time; the constructor allocates two ``deque(maxlen=10000)`` instances
and three counters (``_valid_count`` / ``_invalid_count`` /
``_duplicate_count``) — no DB / network / I/O at construction time.

The HTTP layer exposes ``GET /api/data-validator/stats`` (added to
``api/server.py`` by the W24-4 wiring block) so an operator can poll
the live counters from the workstation dashboard.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a ``validate_snapshot`` / ``validate_trade`` call.

    Attributes:
        is_valid: ``True`` if the record passed every check and should be
            accepted by the downstream recorder. ``False`` if any error
            (missing field / out-of-range value / very-stale timestamp)
            was recorded. Mutually exclusive with ``is_duplicate``.
        is_duplicate: ``True`` if the record was rejected because the
            dedup fast-path matched (snapshot hash already seen / trade
            id already seen). When ``True``, ``is_valid`` is ``False``
            and ``errors`` carries ``["Duplicate ..."]``.
        errors: list of human-readable error strings (empty on success).
        warnings: list of human-readable warning strings (carried through
            to the normalised payload so the downstream recorder can
            surface them in operator logs). Empty on success.
        normalized_data: the validated + normalised payload. Empty dict
            when ``is_valid == False`` or ``is_duplicate == True``.
        ingestion_time: ``time.time()`` captured at the start of the
            validation call. Passed through to the normalised payload
            so every record carries the same provenance timestamp
            (rather than each downstream consumer calling ``time.time()``
            and seeing a slightly different value).
    """

    is_valid: bool
    is_duplicate: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_data: dict[str, Any] = field(default_factory=dict)
    ingestion_time: float = 0.0


class DataValidator:
    """Validates and normalises data at ingestion time.

    Thread-safety
    -------------
    The validator is single-threaded by design — it's invoked from the
    book poller's ``_apply_book`` (a single ``asyncio`` task per token)
    and the trade ingester's ``_ingest_trades`` (a single ``asyncio``
    task per poll). The dedup sets are plain ``deque``s, not
    ``asyncio.Lock``-guarded — concurrent access from two coroutines
    would race. The book poller and trade ingester are both
    cooperative-scheduling ``async`` paths whose validation calls
    complete synchronously (no ``await`` inside ``validate_snapshot``
    / ``validate_trade``), so the GIL keeps the deque mutations atomic
    at the bytecode level.

    If a future wave adds a second consumer (e.g. a second poller
    instance), the validator should be wrapped in an ``asyncio.Lock``
    or split into per-consumer instances.
    """

    def __init__(self, max_seen_ids: int = 10_000) -> None:
        # Bounded deques — when ``maxlen`` is hit, the oldest entries
        # are evicted automatically. Mirrors the ``_MAX_SEEN_TRADE_IDS``
        # / ``_KEEP_SEEN_TRADE_IDS`` pattern in
        # ``core/trade_ingester.py`` (W20-7) and ``core/live_fill_monitor.py``
        # (W18-2) — the durable UNIQUE constraint on ``trade_id`` is the
        # backstop for restarts / replays past the in-memory window.
        self._seen_ids: deque = deque(maxlen=max_seen_ids)
        self._seen_hashes: deque = deque(maxlen=max_seen_ids)
        self._duplicate_count: int = 0
        self._invalid_count: int = 0
        self._valid_count: int = 0

    # ── Snapshot validation ───────────────────────────────────────────────

    def validate_snapshot(self, raw_data: dict) -> ValidationResult:
        """Validate a market snapshot (top-of-book for a single token).

        Args:
            raw_data: the raw snapshot dict. Required fields:
                ``token_id`` (str), ``best_bid`` (float in [0, 1]),
                ``best_ask`` (float in [0, 1]). Optional: ``timestamp``
                (int / float / ISO-8601 string — defaults to
                ``ingestion_time``), ``source`` (str — defaults to
                ``"unknown"``), ``mid`` / ``spread`` (computed from
                best_bid/best_ask if absent).

        Returns:
            ``ValidationResult``. On duplicate: ``is_valid=False``,
            ``is_duplicate=True``. On schema / value / staleness error:
            ``is_valid=False``, ``errors`` populated. On success:
            ``is_valid=True``, ``normalized_data`` carries the input
            fields augmented with ``timestamp`` / ``ingestion_time`` /
            ``processing_time`` / ``source`` / (derived) ``mid`` /
            ``spread``.
        """
        errors: list[str] = []
        warnings: list[str] = []
        ingestion_time = time.time()

        # 1. Deduplication by hash.
        snapshot_hash = self._hash_snapshot(raw_data)
        if snapshot_hash in self._seen_hashes:
            self._duplicate_count += 1
            return ValidationResult(
                is_valid=False,
                is_duplicate=True,
                errors=["Duplicate snapshot"],
                warnings=[],
                normalized_data={},
                ingestion_time=ingestion_time,
            )
        self._seen_hashes.append(snapshot_hash)

        # 2. Required fields.
        required = ["token_id", "best_bid", "best_ask"]
        for field_name in required:
            if field_name not in raw_data:
                errors.append(f"Missing required field: {field_name}")

        # 3. Value validation.
        best_bid = raw_data.get("best_bid", 0)
        best_ask = raw_data.get("best_ask", 0)

        if not _is_in_unit_range(best_bid):
            errors.append(f"Invalid best_bid: {best_bid} (must be 0-1)")
        if not _is_in_unit_range(best_ask):
            errors.append(f"Invalid best_ask: {best_ask} (must be 0-1)")
        # Crossed market is a warning, not an error — book poller
        # legitimately observes crossed books when an aggressive
        # market maker is sweeping both sides.
        try:
            if best_bid > best_ask:
                warnings.append(f"Crossed market: bid {best_bid} > ask {best_ask}")
        except TypeError:
            # ``best_bid`` / ``best_ask`` aren't comparable (one or both
            # are non-numeric) — the value-validation branch above
            # already appended an error, so this is a no-op.
            pass

        # 4. Timestamp normalisation.
        timestamp = raw_data.get("timestamp")
        if timestamp is None:
            timestamp = ingestion_time
            warnings.append("Missing timestamp — using ingestion time")
        elif isinstance(timestamp, str):
            try:
                timestamp = float(timestamp)
            except ValueError:
                # Try ISO-8601 format (e.g. "2026-09-04T12:34:56Z" /
                # "2026-09-04T12:34:56+00:00"). ``datetime.fromisoformat``
                # in Python 3.11+ accepts the trailing "Z" natively; on
                # 3.10 we have to strip it.
                from datetime import datetime

                iso_str = timestamp
                if iso_str.endswith("Z"):
                    iso_str = iso_str[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(iso_str)
                    timestamp = dt.timestamp()
                except ValueError:
                    errors.append(f"Invalid timestamp format: {timestamp}")
                    timestamp = ingestion_time
        elif isinstance(timestamp, (int, float)):
            # Already a numeric type — keep as float.
            timestamp = float(timestamp)
        else:
            errors.append(f"Invalid timestamp type: {type(timestamp).__name__}")
            timestamp = ingestion_time

        # 5. Staleness check. The very-stale branch (>= 300s) is checked
        # FIRST so a 600s-old record is rejected, not just warned about.
        # (Original W24-4 spec had the elif order reversed — the warning
        # branch matched first, masking the rejection. Fixed here.)
        try:
            staleness = ingestion_time - float(timestamp)
        except (TypeError, ValueError):
            staleness = 0.0
        if staleness > 300:
            errors.append(f"Very stale data: {staleness:.1f}s old — rejecting")
        elif staleness > 60:
            warnings.append(f"Stale data: {staleness:.1f}s old")

        if errors:
            self._invalid_count += 1
            return ValidationResult(
                is_valid=False,
                is_duplicate=False,
                errors=errors,
                warnings=warnings,
                normalized_data={},
                ingestion_time=ingestion_time,
            )

        # Normalise — preserve every input field, override the timestamp
        # with the normalised value, add provenance fields.
        normalized: dict[str, Any] = {
            **raw_data,
            "timestamp": timestamp,
            "ingestion_time": ingestion_time,
            "processing_time": time.time(),
            "source": raw_data.get("source", "unknown"),
        }

        # Derived fields — only compute if both sides are numeric and
        # missing from the input.
        try:
            if "mid" not in normalized and best_bid and best_ask:
                normalized["mid"] = (best_bid + best_ask) / 2
            if "spread" not in normalized and best_bid is not None and best_ask is not None:
                normalized["spread"] = best_ask - best_bid
        except TypeError:
            # ``best_bid`` / ``best_ask`` aren't numeric — the
            # value-validation branch above already appended an error,
            # so this branch is unreachable in practice (we'd have
            # returned early). Defensive only.
            pass

        self._valid_count += 1
        return ValidationResult(
            is_valid=True,
            is_duplicate=False,
            errors=[],
            warnings=warnings,
            normalized_data=normalized,
            ingestion_time=ingestion_time,
        )

    # ── Trade validation ──────────────────────────────────────────────────

    def validate_trade(self, raw_data: dict) -> ValidationResult:
        """Validate a single trade record.

        Args:
            raw_data: the raw trade dict. Required fields:
                ``token_id`` (str), ``price`` (float > 0, ideally
                ``<= 1.0``), ``size`` (float > 0), ``side`` (``"BUY"``
                or ``"SELL"``, case-insensitive). Optional:
                ``trade_id`` / ``id`` (str — dedup key, defaults to
                empty string which skips dedup), ``timestamp``
                (int / float / ISO-8601 string — defaults to
                ``ingestion_time``), ``maker_address``,
                ``taker_order_id`` (passed through unchanged).

        Returns:
            ``ValidationResult``. On duplicate ``trade_id``:
            ``is_valid=False``, ``is_duplicate=True``. On schema /
            value error: ``is_valid=False``, ``errors`` populated.
            On success: ``is_valid=True``, ``normalized_data``
            carries the input fields with ``price`` / ``size`` /
            ``timestamp`` coerced to ``float``, ``side`` upper-cased
            to ``BUY`` / ``SELL``, and provenance fields added.
        """
        errors: list[str] = []
        warnings: list[str] = []
        ingestion_time = time.time()

        # 1. Deduplication by trade_id.
        trade_id = raw_data.get("trade_id") or raw_data.get("id", "")
        if trade_id and trade_id in self._seen_ids:
            self._duplicate_count += 1
            return ValidationResult(
                is_valid=False,
                is_duplicate=True,
                errors=["Duplicate trade"],
                warnings=[],
                normalized_data={},
                ingestion_time=ingestion_time,
            )
        if trade_id:
            self._seen_ids.append(trade_id)

        # 2. Required fields.
        for field_name in ["token_id", "price", "size", "side"]:
            if field_name not in raw_data:
                errors.append(f"Missing required field: {field_name}")

        # 3. Value validation.
        price = raw_data.get("price", 0)
        size = raw_data.get("size", 0)
        side = raw_data.get("side", "")
        # Normalise side to upper-case for both the validation check
        # and the normalised payload. Empty string is rejected by the
        # ``not in`` check below.
        side_norm = str(side).upper() if side else ""

        try:
            price_f = float(price)
        except (TypeError, ValueError):
            price_f = -1.0  # force the ``<= 0`` error branch below
        try:
            size_f = float(size)
        except (TypeError, ValueError):
            size_f = -1.0

        if price_f <= 0:
            errors.append(f"Invalid price: {price}")
        if price_f > 1.0:
            warnings.append(
                f"Price > 1.0: {price} — unusual for prediction market"
            )
        if size_f <= 0:
            errors.append(f"Invalid size: {size}")
        if side_norm not in ("BUY", "SELL"):
            errors.append(f"Invalid side: {side}")

        if errors:
            self._invalid_count += 1
            return ValidationResult(
                is_valid=False,
                is_duplicate=False,
                errors=errors,
                warnings=warnings,
                normalized_data={},
                ingestion_time=ingestion_time,
            )

        # Normalise — preserve every input field, override the
        # price/size/side/timestamp with normalised values, add
        # provenance fields.
        normalized: dict[str, Any] = {
            **raw_data,
            "trade_id": trade_id,
            "price": price_f,
            "size": size_f,
            "side": side_norm,
            "ingestion_time": ingestion_time,
            "processing_time": time.time(),
        }

        # Timestamp normalisation. Trades don't run the staleness
        # check (a stale trade is still a valid historical fill — the
        # operator may be back-filling the tape); we only normalise
        # the format.
        timestamp = raw_data.get("timestamp")
        if timestamp is None:
            timestamp = ingestion_time
        elif isinstance(timestamp, str):
            try:
                timestamp = float(timestamp)
            except ValueError:
                from datetime import datetime

                iso_str = timestamp
                if iso_str.endswith("Z"):
                    iso_str = iso_str[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(iso_str)
                    timestamp = dt.timestamp()
                except ValueError:
                    # Fall back to ingestion_time so the row still
                    # has a numeric timestamp (rather than rejecting
                    # the trade entirely for a malformed timestamp).
                    warnings.append(
                        f"Invalid timestamp format: {timestamp} — using ingestion time"
                    )
                    timestamp = ingestion_time
        elif isinstance(timestamp, (int, float)):
            timestamp = float(timestamp)
        else:
            warnings.append(
                f"Invalid timestamp type: {type(timestamp).__name__} — using ingestion time"
            )
            timestamp = ingestion_time
        normalized["timestamp"] = timestamp

        self._valid_count += 1
        return ValidationResult(
            is_valid=True,
            is_duplicate=False,
            errors=[],
            warnings=warnings,
            normalized_data=normalized,
            ingestion_time=ingestion_time,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _hash_snapshot(self, data: dict) -> str:
        """Create a 16-char sha256 hash for snapshot deduplication.

        Hashes the (token_id, best_bid, best_ask, timestamp) tuple so
        two snapshots of the same token with the same top-of-book +
        timestamp are deduplicated. The hash is truncated to 16 hex
        chars (64 bits) — collision probability is ~1 in 10^19 for a
        10k-entry dedup window, which is acceptable for an in-memory
        fast-path (the durable UNIQUE constraint on the DB is the
        backstop).
        """
        key_fields = ["token_id", "best_bid", "best_ask", "timestamp"]
        key_data = {k: data.get(k) for k in key_fields}
        # ``sorted`` on the items list gives a stable order regardless
        # of dict insertion order (Python 3.7+ preserves insertion order
        # but we hash against the canonical sorted form so two dicts
        # with the same keys in different insertion orders hash equal).
        return hashlib.sha256(
            str(sorted(key_data.items())).encode()
        ).hexdigest()[:16]

    def get_stats(self) -> dict[str, Any]:
        """Return live validator counters.

        Exposed via ``GET /api/data-validator/stats`` (W24-4 wiring
        block in ``api/server.py``) so an operator dashboard can poll
        the dedup / valid / invalid counts without a DB round-trip.

        Returns:
            Dict with keys: ``valid_count``, ``invalid_count``,
            ``duplicate_count``, ``seen_ids_size``, ``seen_hashes_size``.
            All values are plain ints (JSON-serialisable). The
            ``seen_*_size`` values reflect the current size of the
            in-memory dedup deques (capped at ``max_seen_ids``).
        """
        return {
            "valid_count": self._valid_count,
            "invalid_count": self._invalid_count,
            "duplicate_count": self._duplicate_count,
            "seen_ids_size": len(self._seen_ids),
            "seen_hashes_size": len(self._seen_hashes),
        }


# ── Module-level helpers ────────────────────────────────────────────────────


def _is_in_unit_range(value: Any) -> bool:
    """Return True iff ``value`` is a numeric in ``[0.0, 1.0]``.

    Non-numeric inputs (``None`` / strings / dicts / lists) return
    ``False`` so the caller can append a single "Invalid ..."
    error rather than choking on a ``TypeError`` from the ``< 0``
    comparison.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 <= f <= 1.0


# ── Module-level singleton ─────────────────────────────────────────────────
# Mirrors the convention used by every sibling background-task module
# (``core.book_poller.book_poller``, ``core.data_quality.data_quality_monitor``,
# ``core.clob_client.clob_client`` …). Importers grab it at module-import
# time; the constructor allocates two ``deque(maxlen=10000)`` instances and
# three counters — no DB / network / I/O at construction time.
data_validator = DataValidator()


__all__ = [
    "DataValidator",
    "ValidationResult",
    "data_validator",
]
