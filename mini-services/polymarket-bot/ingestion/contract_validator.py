"""Data contract validator — ensures ingestion output matches ML input expectations.

Validates:
1. Feature schema: All required features present, correct types
2. Value ranges: Prices 0-1, sizes > 0, timestamps reasonable
3. Point-in-time correctness: No future data leakage
4. Training/live compatibility: Same features used in both
5. Version compatibility: Feature version matches model version

W33-3 — Sits between the W24-4 ``DataValidator`` (which validates the
RAW source payload) and the ML ``MarketMLModel.predict()`` path (which
consumes the normalized + enriched + feature-derived output). Two
public entry points:

* :meth:`DataContractValidator.validate` — validates the normalized
  payload against a named data contract (``"market_snapshot"`` /
  ``"trade"``). Called by :class:`ingestion.pipeline.Pipeline` AFTER
  normalization, BEFORE enrichment so a contract violation short-
  circuits the pipeline before any derived field is computed (and
  before the record reaches the ML feature store). On violation the
  record is reclassified as ``quality_state="invalid"`` and forwarded
  to the dead-letter queue with ``reason="contract_violation"``.

* :meth:`DataContractValidator.validate_features` — validates the
  38-feature vector against the W31-6 ``FEATURE_CONTRACTS`` catalog.
  Called by :meth:`ml.model.MarketMLModel.predict` BEFORE the model's
  inference path so a malformed feature vector (wrong length, NaN /
  Inf, out-of-range value, training/live schema mismatch) returns a
  neutral ``(0.5, 0.0)`` prediction rather than feeding the model an
  out-of-distribution input.

Design
------
Both methods are pure (no I/O, no raises) so they can be called from
the pipeline / predict hot path without a try/except wrapper. The
validator maintains live counters (``checked_count`` /
``valid_count`` / ``invalid_count``) surfaced via :meth:`get_stats`
so the operator dashboard can report contract-violation rates next
to the existing W24-4 valid / invalid / duplicate counters.

The :class:`FieldSpec` dataclass is the unit of contract definition
(name + Python type + value bounds + required flag). Each named
contract (``market_snapshot`` / ``trade``) is a ``dict[str,
FieldSpec]`` keyed by the field name. Adding a new contract is a
matter of authoring a new ``FieldSpec`` dict + registering it in
``_CONTRACTS`` — no class change needed.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Lazy imports for the ML feature catalog ──────────────────────────────────
# Imported defensively so this module is import-safe even if the ML
# package is partially unavailable in the test sandbox (a sibling wave
# might not have landed its file yet, or the ``ml.features`` import
# might transitively require a dependency that's missing in the env).
# The lazy fallbacks mean the contract validator still works for the
# ingestion-time contracts (``market_snapshot`` / ``trade``) — only the
# per-feature range check on the ML feature vector is skipped when the
# catalog is unavailable.
try:
    from ml.features import FEATURE_NAMES, N_FEATURES  # type: ignore
except ImportError:  # pragma: no cover — defensive
    FEATURE_NAMES = None  # type: ignore[assignment]
    N_FEATURES = None  # type: ignore[assignment]

try:
    from ingestion.feature_contracts import (  # type: ignore
        FEATURE_CONTRACTS,
        TYPE_BOOLEAN,
        TYPE_NUMERIC,
    )
except ImportError:  # pragma: no cover — defensive
    FEATURE_CONTRACTS = None  # type: ignore[assignment]
    TYPE_BOOLEAN = "boolean"
    TYPE_NUMERIC = "numeric"


# ── Contract names ──────────────────────────────────────────────────────────
CONTRACT_MARKET_SNAPSHOT = "market_snapshot"
CONTRACT_TRADE = "trade"
CONTRACT_FEATURE_VECTOR = "feature_vector"

# ── Point-in-time thresholds ────────────────────────────────────────────────
# Mirrors the W24-4 ``data_validator`` staleness threshold (past
# direction) so the contract's PIT check doesn't drift from the
# validator's own staleness gate. The future direction is stricter
# (5s tolerance for clock skew between source and consumer) — a
# timestamp MORE than 5s in the future is almost certainly a future-
# data leak (the source's clock is wrong, or someone is replaying
# future timestamps into a backtest).
PAST_STALE_THRESHOLD_S = 300.0
FUTURE_LEAK_THRESHOLD_S = 5.0


# ── Result dataclass ────────────────────────────────────────────────────────
@dataclass
class ContractResult:
    """Result of a contract validation call.

    Attributes:
        contract: Name of the contract that was validated against
            (``"market_snapshot"`` / ``"trade"`` / ``"feature_vector"``).
        is_valid: ``True`` iff every required field passed schema +
            range + PIT checks.
        errors: Human-readable violation messages, one per failed
            check. Joined by ``"; "`` when surfaced in pipeline /
            DLQ error_reason fields.
        warnings: Non-blocking warnings (e.g. crossed market on a
            snapshot, missing catalog entry for a feature). Always
            surfaced via ``get_stats()["last_errors"]`` for telemetry
            but never block the record.
        checked_fields: Number of fields the validator inspected (for
            telemetry — a low value on a ``feature_vector`` contract
            indicates the per-feature loop was skipped due to an
            early length / version error).
    """

    contract: str
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_fields: int = 0

    def __bool__(self) -> bool:
        """Truthiness mirrors ``is_valid`` so callers can write
        ``if contract_result:`` rather than ``if contract_result.is_valid:``.
        """
        return self.is_valid


# ── Per-field contract spec ─────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldSpec:
    """Schema for a single field within a data contract.

    A ``FieldSpec`` is the unit of contract definition: it pairs a
    field ``name`` with its expected Python ``type``, value bounds
    (``min_value`` / ``max_value`` — only meaningful for numeric
    types), a ``required`` flag, and a human-readable ``description``.
    Frozen so a contract can be shared across validator instances
    without fear of mutation.

    The bounds are inclusive — a ``min_value=0.0`` field accepts ``0.0``.
    """

    name: str
    type: type  # Python type — int / float / str / bool
    required: bool = True
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allow_nan: bool = False
    description: str = ""


# ── market_snapshot contract ───────────────────────────────────────────────
# Maps the fields the W24-4 ``data_validator.validate_snapshot`` normalizes
# the raw snapshot to. ``best_bid`` / ``best_ask`` are the source fields;
# ``mid`` / ``spread`` are derived fields the W24-4 validator adds when
# both sides are numeric. The contract validator's job is to confirm
# the post-normalization payload still carries every field the downstream
# ML feature extractor depends on (``ml.features.extract_features`` reads
# ``mid`` from the order book, which is built from ``best_bid`` /
# ``best_ask``).
MARKET_SNAPSHOT_FIELDS: dict[str, FieldSpec] = {
    "token_id": FieldSpec(
        name="token_id",
        type=str,
        required=True,
        description="Unique market token identifier (Polymarket condition_id).",
    ),
    "best_bid": FieldSpec(
        name="best_bid",
        type=float,
        required=True,
        min_value=0.0,
        max_value=1.0,
        description="Best bid price (0-1 inclusive — Polymarket prices are shares-of-1).",
    ),
    "best_ask": FieldSpec(
        name="best_ask",
        type=float,
        required=True,
        min_value=0.0,
        max_value=1.0,
        description="Best ask price (0-1 inclusive).",
    ),
    "mid": FieldSpec(
        name="mid",
        type=float,
        required=False,
        min_value=0.0,
        max_value=1.0,
        description="Mid price (best_bid + best_ask) / 2 — derived by the W24-4 validator.",
    ),
    "spread": FieldSpec(
        name="spread",
        type=float,
        required=False,
        min_value=0.0,
        description="Bid-ask spread (best_ask - best_bid) — derived by the W24-4 validator.",
    ),
    "timestamp": FieldSpec(
        name="timestamp",
        type=float,
        required=True,
        description="Unix-epoch seconds — the source-reported event_time.",
    ),
}


# ── trade contract ─────────────────────────────────────────────────────────
TRADE_FIELDS: dict[str, FieldSpec] = {
    "token_id": FieldSpec(
        name="token_id",
        type=str,
        required=True,
        description="Unique market token identifier.",
    ),
    "price": FieldSpec(
        name="price",
        type=float,
        required=True,
        min_value=0.0,
        max_value=1.0,
        description="Trade price (0-1 inclusive for prediction markets).",
    ),
    "size": FieldSpec(
        name="size",
        type=float,
        required=True,
        min_value=0.0,
        description="Trade size (shares). 0 is allowed (some exchanges report 0-size cancels).",
    ),
    "side": FieldSpec(
        name="side",
        type=str,
        required=True,
        description='"BUY" or "SELL" (case-insensitive — the W24-4 validator upper-cases).',
    ),
    "timestamp": FieldSpec(
        name="timestamp",
        type=float,
        required=True,
        description="Unix-epoch seconds — the source-reported event_time.",
    ),
}


# ── Contract registry ───────────────────────────────────────────────────────
_CONTRACTS: dict[str, dict[str, FieldSpec]] = {
    CONTRACT_MARKET_SNAPSHOT: MARKET_SNAPSHOT_FIELDS,
    CONTRACT_TRADE: TRADE_FIELDS,
}


# ── Feature-version compatibility map ──────────────────────────────────────
# Maps a model-version PREFIX (e.g. ``"v1"``) to the feature count the
# model was trained against. The validator's
# :meth:`DataContractValidator.validate_features` call checks this map
# before running per-feature range checks so a model trained against a
# 38-feature catalog that's fed a 40-feature vector (e.g. a sibling wave
# added 2 features and forgot to retrain) is flagged as a schema
# mismatch BEFORE the model is invoked.
#
# The "v1" prefix covers every ``v1.X.Y`` SemVer string minted by
# :meth:`MarketMLModel.fit_initial` (which uses
# ``f"v1.{int(time.time()) % 1000:03d}.0"``). A future "v2" model
# would need to register its expected feature count here.
_FEATURE_COUNT_BY_VERSION_PREFIX: dict[str, int] = {
    "v1": 38,  # the W11-era 38-feature catalog (ml.features.N_FEATURES)
}


def _event_type_to_contract(event_type: str) -> Optional[str]:
    """Map a pipeline event_type to the data contract name.

    Returns ``None`` for event types that don't have a contract
    (``"order_book"`` / ``"market_info"`` / ``"news"``) so the
    pipeline can short-circuit the contract check for those — they
    skip the W24-4 validator too (the W24-4 validator only knows
    snapshots + trades), so the post-normalization contract check
    would have nothing meaningful to validate.
    """
    if event_type == "snapshot":
        return CONTRACT_MARKET_SNAPSHOT
    if event_type == "trade":
        return CONTRACT_TRADE
    return None


# ── Helpers ─────────────────────────────────────────────────────────────────
def _is_type(value: Any, expected: type) -> bool:
    """Strict type check that rejects bool-as-int / bool-as-float.

    Python's ``isinstance(True, int)`` returns ``True`` because
    ``bool`` is a subclass of ``int`` — that would silently accept
    ``True`` for a ``float`` field and coerce it to ``1.0``. The
    contract validator must reject this so a JSON
    ``"best_bid": true`` is flagged as a schema violation rather
    than being silently accepted as ``1.0`` (which would then pass
    the [0, 1] range check and reach the ML model as a "price = 1.0"
    feature — almost certainly a bug, not a real market state).

    For numeric ``expected`` types, we accept int OR float
    interchangeably (so a JSON ``"best_bid": 0`` is accepted and
    coerced to ``0.0`` downstream).
    """
    if expected is float:
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float))
    if expected is int:
        if isinstance(value, bool):
            return False
        return isinstance(value, int)
    # Numpy scalar types — accept np.float64 (subclass of Python float)
    # and np.int64 (NOT a subclass of Python int, so accept explicitly).
    if expected in (float, int):
        try:
            import numpy as np  # local import — keep the module numpy-free at import time
            if isinstance(value, (np.floating, np.integer)):
                return True
        except ImportError:  # pragma: no cover — defensive
            pass
    return isinstance(value, expected)


def _is_in_range(
    value: float,
    min_value: Optional[float],
    max_value: Optional[float],
    allow_nan: bool,
) -> bool:
    """Range check with NaN / Inf handling.

    A NaN or Inf value is rejected unless ``allow_nan=True`` (no
    field in the current contract catalog sets ``allow_nan=True`` —
    the flag is here for future derived fields that might tolerate
    NaN, e.g. an optional mid_price when the book is one-sided).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v) or math.isinf(v):
        return allow_nan
    if min_value is not None and v < min_value:
        return False
    if max_value is not None and v > max_value:
        return False
    return True


def _render_bounds(min_value: Optional[float], max_value: Optional[float]) -> str:
    """Render a human-readable bound string for an error message."""
    if min_value is not None and max_value is not None:
        return f"[{min_value}, {max_value}]"
    if min_value is not None:
        return f">= {min_value}"
    if max_value is not None:
        return f"<= {max_value}"
    return "any"


def _version_prefix(version: str) -> str:
    """Extract the major-version prefix from a SemVer-ish string.

    ``"v1.0.0"`` → ``"v1"``
    ``"v2.3.1"`` → ``"v2"``
    ``"1.0.0"``  → ``"v1"`` (no leading 'v' — treated as v{major})

    Returns ``"v0"`` for an unparseable string so the validator
    can emit a ``warnings`` entry for an unknown prefix rather
    than crashing.
    """
    s = (version or "").strip()
    if not s:
        return "v0"
    if s.startswith("v") or s.startswith("V"):
        s = s[1:]
    parts = s.split(".")
    if not parts or not parts[0]:
        return "v0"
    return "v" + parts[0]


def _feature_value_bounds(ftype: str) -> tuple[float, float]:
    """Conservative (min, max) bounds for a feature given its contract type.

    The 38-feature catalog mixes:
      * price-derived features in [0, 1] (mid_price, spread_norm, vol_log, …)
      * signed imbalance features in [-1, 1] (ofi, micro_price_drift, …)
      * boolean regime one-hot flags in {0, 1}
      * time cyclical features in [-1, 1] (hour_sin/cos, day_sin/cos)

    A handful of features (e.g. ``hurst_exponent``) are in [0, 1]
    rather than [-1, 1], but the wider [-1, 1] bound still accepts
    them — we optimize for catching OUT-of-distribution values
    (e.g. a NaN, a > 1.5 mid_price from a fat-finger fill that
    crossed 1.0) rather than tight per-feature validation.

    If a real production feature ever legitimately falls outside
    these bounds, the contract for that feature should be updated
    (and the feature's version bumped) rather than widening the
    bound here.
    """
    if ftype == TYPE_BOOLEAN:
        return (0.0, 1.0)
    return (-1.0, 1.0)


# ── Validator class ─────────────────────────────────────────────────────────
class DataContractValidator:
    """Validates data against named contracts before it reaches the ML model.

    Two public entry points (both pure / no-raises):

    * :meth:`validate` — ingestion-time check on the normalized payload.
    * :meth:`validate_features` — ML predict-path check on the feature
      vector.

    The class maintains live counters (``checked_count`` /
    ``valid_count`` / ``invalid_count``) surfaced via :meth:`get_stats`
    so the operator dashboard can report contract-violation rates next
    to the existing W24-4 valid / invalid / duplicate counters. Mirrors
    the convention used by :class:`core.data_validator.DataValidator`.
    """

    def __init__(self) -> None:
        self._checked_count: int = 0
        self._valid_count: int = 0
        self._invalid_count: int = 0
        # Rolling view of the most recent violations (capped at 50).
        # Surfaced via ``get_stats()["last_errors"]`` so an operator
        # can spot-check the latest contract violations without
        # scraping the dead-letter queue.
        self._last_errors: list[str] = []

    # ── Public: stats / reset ───────────────────────────────────────────────
    def get_stats(self) -> dict[str, Any]:
        """Live counters + last-N errors (JSON-serialisable).

        Mirrors the shape of :meth:`DataValidator.get_stats` so the
        operator dashboard's "data quality" card can render this next
        to the existing valid/invalid/duplicate rows.
        """
        return {
            "checked_count": self._checked_count,
            "valid_count": self._valid_count,
            "invalid_count": self._invalid_count,
            "last_errors": list(self._last_errors[-10:]),
        }

    def reset_stats(self) -> None:
        """Zero the counters + clear the rolling error view (test-only)."""
        self._checked_count = 0
        self._valid_count = 0
        self._invalid_count = 0
        self._last_errors.clear()

    # ── Public: ingestion-time contract check ──────────────────────────────
    def validate(self, data: Any, contract: str) -> ContractResult:
        """Validate ``data`` against the named ``contract``.

        Args:
            data: The normalized payload (a dict). Non-dict inputs
                are rejected — the contract requires field access.
            contract: ``"market_snapshot"`` or ``"trade"``. Unknown
                contract names are rejected with an explicit error.

        Returns:
            :class:`ContractResult`. Never raises — the caller can
            branch on ``is_valid`` and inspect ``errors`` for the
            violation detail. ``warnings`` are non-blocking.
        """
        self._checked_count += 1

        if contract not in _CONTRACTS:
            err = f"Unknown contract: {contract!r}"
            self._record_invalid([err])
            return ContractResult(
                contract=contract,
                is_valid=False,
                errors=[err],
                checked_fields=0,
            )

        if not isinstance(data, dict):
            err = (
                f"Contract {contract!r} expects a dict payload, got "
                f"{type(data).__name__}"
            )
            self._record_invalid([err])
            return ContractResult(
                contract=contract,
                is_valid=False,
                errors=[err],
                checked_fields=0,
            )

        fields = _CONTRACTS[contract]
        errors: list[str] = []
        warnings: list[str] = []
        checked = 0

        for spec in fields.values():
            checked += 1
            if spec.name not in data:
                if spec.required:
                    errors.append(
                        f"[{contract}] Missing required field: {spec.name}"
                    )
                continue
            value = data[spec.name]
            # Type check — reject bool-as-int / bool-as-float explicitly.
            if not _is_type(value, spec.type):
                errors.append(
                    f"[{contract}] Field {spec.name!r} wrong type: "
                    f"expected {spec.type.__name__}, got "
                    f"{type(value).__name__}"
                )
                # Skip the range check — wrong-type means we can't
                # reason about the value's range safely.
                continue
            # Range check (numeric types only).
            if spec.type in (int, float):
                if not _is_in_range(
                    value, spec.min_value, spec.max_value, spec.allow_nan
                ):
                    bound = _render_bounds(spec.min_value, spec.max_value)
                    errors.append(
                        f"[{contract}] Field {spec.name!r} out of range: "
                        f"{value!r} (must be {bound})"
                    )

        # ── Per-contract cross-field invariants ────────────────────────────
        if contract == CONTRACT_TRADE:
            side = data.get("side")
            if isinstance(side, str) and side.upper() not in ("BUY", "SELL"):
                errors.append(
                    f"[trade] Field 'side' invalid enum: {side!r} "
                    f"(must be BUY or SELL)"
                )

        if contract == CONTRACT_MARKET_SNAPSHOT:
            bb = data.get("best_bid")
            ba = data.get("best_ask")
            # Reject bool explicitly (already rejected above, but
            # belt-and-braces here so a future refactor that loosens
            # the type check doesn't silently accept ``best_bid=True``).
            if (
                isinstance(bb, (int, float))
                and isinstance(ba, (int, float))
                and not isinstance(bb, bool)
                and not isinstance(ba, bool)
            ):
                if bb > ba:
                    warnings.append(
                        f"[market_snapshot] Crossed market: bid {bb} > ask {ba}"
                    )

        # ── Point-in-time correctness — reject future timestamps ──────────
        # Past timestamps are already gated by the pipeline's staleness
        # override (W31-1 ``STALE_REJECT_THRESHOLD_S = 300.0``), so we
        # only flag the future direction here. A timestamp MORE than
        # ``FUTURE_LEAK_THRESHOLD_S`` (5 s) in the future is almost
        # certainly a future-data leak — the source's clock is wrong,
        # or someone is replaying future timestamps into a backtest.
        ts = data.get("timestamp")
        if (
            isinstance(ts, (int, float))
            and not isinstance(ts, bool)
        ):
            now = time.time()
            try:
                ts_f = float(ts)
                if ts_f > now + FUTURE_LEAK_THRESHOLD_S:
                    errors.append(
                        f"[{contract}] Future timestamp: {ts_f} > "
                        f"now+{FUTURE_LEAK_THRESHOLD_S}s "
                        f"({now + FUTURE_LEAK_THRESHOLD_S:.1f}) — "
                        f"possible future-data leak"
                    )
            except (TypeError, ValueError):
                # Already covered by the type check above.
                pass

        is_valid = not errors
        if is_valid:
            self._valid_count += 1
        else:
            self._record_invalid(errors)

        return ContractResult(
            contract=contract,
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            checked_fields=checked,
        )

    # ── Public: ML feature vector contract check ───────────────────────────
    def validate_features(
        self,
        features: Any,
        model_version: Optional[str] = None,
    ) -> ContractResult:
        """Validate the ML feature vector before it's fed to ``predict()``.

        Args:
            features: ``np.ndarray`` of length 38 (or any sequence with
                a ``len()``). The validator checks the length against
                ``N_FEATURES`` (the W11-era 38-feature catalog) and
                inspects each value against the per-feature contract
                from :data:`ingestion.feature_contracts.FEATURE_CONTRACTS`.
            model_version: Model-registry version string (e.g.
                ``"v1.0.0"``). When supplied, the validator checks
                the version prefix against
                :data:`_FEATURE_COUNT_BY_VERSION_PREFIX` and confirms
                the feature count matches the model's expected count
                — catches the train/serve schema-mismatch failure
                mode where the catalog grew but the cached model
                wasn't retrained.

        Returns:
            :class:`ContractResult` with ``contract="feature_vector"``.
            Never raises — a TypeError / IndexError inside the
            per-feature loop is caught and converted into an error
            entry rather than propagated.
        """
        self._checked_count += 1
        errors: list[str] = []
        warnings: list[str] = []
        checked = 0

        # ── Length check ──────────────────────────────────────────────────
        try:
            n = len(features)  # type: ignore[arg-type]
        except TypeError:
            err = (
                "[feature_vector] Features has no len() — "
                f"got {type(features).__name__}"
            )
            self._record_invalid([err])
            return ContractResult(
                contract=CONTRACT_FEATURE_VECTOR,
                is_valid=False,
                errors=[err],
                checked_fields=0,
            )

        checked += 1
        expected_n = N_FEATURES if N_FEATURES is not None else 38
        if n != expected_n:
            errors.append(
                f"[feature_vector] Feature count mismatch: got {n}, "
                f"expected {expected_n}"
            )

        # ── Version compatibility check ──────────────────────────────────
        if model_version is not None:
            checked += 1
            prefix = _version_prefix(model_version)
            expected = _FEATURE_COUNT_BY_VERSION_PREFIX.get(prefix)
            if expected is None:
                warnings.append(
                    f"[feature_vector] Unknown model_version prefix: "
                    f"{model_version!r} (no feature-count contract for "
                    f"prefix {prefix!r})"
                )
            elif expected != expected_n:
                errors.append(
                    f"[feature_vector] Version {model_version!r} expects "
                    f"{expected} features but catalog has {expected_n} — "
                    f"training/live schema mismatch"
                )

        # ── Per-feature type + range check ────────────────────────────────
        # Skipped when:
        #   * the FEATURE_CONTRACTS catalog is unavailable (defensive import failed);
        #   * a length / version error already fired (skip the per-feature
        #     loop so we don't double-error on every index past the end).
        if FEATURE_CONTRACTS is not None and FEATURE_NAMES is not None and not errors:
            try:
                for i, name in enumerate(FEATURE_NAMES):
                    checked += 1
                    if i >= n:
                        # Already covered by the length check above —
                        # break out rather than double-erroring.
                        break
                    value = float(features[i])  # type: ignore[index]
                    contract = FEATURE_CONTRACTS.get(name)
                    if contract is None:
                        warnings.append(
                            f"[feature_vector] Feature at index {i} "
                            f"({name!r}) has no catalog entry — "
                            f"skipping range check"
                        )
                        continue
                    # NaN / Inf check (every feature contract rejects these).
                    if math.isnan(value) or math.isinf(value):
                        errors.append(
                            f"[feature_vector] Feature {name!r} (index {i}) "
                            f"is NaN/Inf: {value}"
                        )
                        continue
                    # Range check — bounds derived from the contract's type.
                    lo, hi = _feature_value_bounds(contract.type)
                    if value < lo or value > hi:
                        errors.append(
                            f"[feature_vector] Feature {name!r} (index {i}) "
                            f"out of range: {value} (must be [{lo}, {hi}])"
                        )
            except (TypeError, ValueError, IndexError) as e:
                errors.append(
                    f"[feature_vector] Could not inspect per-feature "
                    f"values: {type(e).__name__}: {e}"
                )

        is_valid = not errors
        if is_valid:
            self._valid_count += 1
        else:
            self._record_invalid(errors)

        return ContractResult(
            contract=CONTRACT_FEATURE_VECTOR,
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            checked_fields=checked,
        )

    # ── Internal helpers ───────────────────────────────────────────────────
    def _record_invalid(self, errors: list[str]) -> None:
        """Bump the invalid counter + append to the rolling error view."""
        self._invalid_count += 1
        self._last_errors.extend(errors)
        # Cap the rolling view so a long-running process doesn't grow
        # the list unbounded. Mirrors the W24-4 deque maxlen pattern.
        if len(self._last_errors) > 50:
            self._last_errors = self._last_errors[-50:]


# ── Module-level singleton ──────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion module
# (``raw_vault`` / ``dead_letter_queue`` / ``checkpoint_manager`` …).
# Construction is cheap (no I/O) so the import-time cost is negligible;
# importers grab the singleton at module-import time and the same
# instance is shared across the pipeline + ML predict path so the
# counters reflect the global contract-violation rate.
contract_validator = DataContractValidator()


__all__ = [
    "ContractResult",
    "DataContractValidator",
    "contract_validator",
    "FieldSpec",
    "MARKET_SNAPSHOT_FIELDS",
    "TRADE_FIELDS",
    "CONTRACT_MARKET_SNAPSHOT",
    "CONTRACT_TRADE",
    "CONTRACT_FEATURE_VECTOR",
    "PAST_STALE_THRESHOLD_S",
    "FUTURE_LEAK_THRESHOLD_S",
    "event_type_to_contract",
]


# Public alias — pipeline imports this via ``from ingestion.contract_validator
# import event_type_to_contract`` so the event_type → contract mapping
# is owned by the contract_validator module (single source of truth).
event_type_to_contract = _event_type_to_contract
