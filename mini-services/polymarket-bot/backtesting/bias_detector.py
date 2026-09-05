"""Backtest bias and leakage detector.

W37-3 — Standalone bias / leakage detector that runs **after** a backtest
(or any time series ML pipeline) and reports every category of statistical
cheating that could make the backtest look better than live trading will
actually be.

Detects (each maps to one ``detect_*`` method + one ``BL_NN`` rule id):

1. **Look-ahead bias** (``BL_01``) — Using future data in current
   decisions. The single most common backtest sin: a feature / label /
   ``p_model`` timestamp strictly later than the decision time at which
   the strategy consumed it.
2. **Data leakage** (``BL_02``) — Training data appearing in the test
   set (a row index — or a (timestamp, token_id) composite key — present
   in both train and test partitions).
3. **Optimistic fills** (``BL_03``) — Assuming fills at unrealistic
   prices: BUY fills strictly below the period ``best_bid`` (you can't
   buy below the bid in a CLOB without crossing the spread) or SELL
   fills strictly above the period ``best_ask`` (symmetric). Also flags
   fills that match the period low / high to micro-dollar tolerance
   (only achievable with future knowledge of the price path).
4. **Future information** (``BL_04``) — Using data not available at
   decision time (a feature value whose ``as_of`` timestamp is later
   than the decision timestamp). Subsumes ``BL_01`` for explicit
   feature-timestamp columns; the two checks run independently so a
   caller can fire either / both.
5. **Survivorship bias** (``BL_05``) — Only testing on markets that
   survived (resolved YES / didn't get delisted). When the test set's
   market-resolution distribution is materially skewed vs. the universe
   of all markets, the backtest is unrepresentative.
6. **Selection bias** (``BL_06``) — Cherry-picking favourable scenarios:
   the strategy_id string itself encodes a positive-selection signal
   (``"winners_only"`` / ``"best_performers"`` / ``"cherry"``) that
   suggests the backtest universe was curated.
7. **Hindsight filtering** (``BL_07``) — Using outcome knowledge in
   entry: the strategy's signal at decision time matches the realized
   outcome with suspiciously high precision (> 0.95 over > 30 trades).
8. **Timestamp leakage** (``BL_08``) — Feature / label timestamps
   overlap between train and test partitions: the train window's max
   timestamp is >= the test window's min timestamp. The
   ``OutOfSampleValidator`` purge + embargo is the canonical fix;
   this check is the belt-and-braces detector for any caller that
   bypasses the validator.
9. **Duplicate participation** (``BL_09``) — Same data point (a
   hashable record id) in both train and test sets. Subtly different
   from ``BL_02``: ``BL_02`` checks index-set intersection (which a
   careful split never violates by construction); ``BL_09`` checks
   record-content intersection (which catches the case where two
   *different* indices reference the same underlying observation — a
   common bug when a feature is re-materialised from a denormalised
   join).
10. **Unrealistic capital reuse** (``BL_10``) — Reusing capital before
    settlement: a BUY trade entered at timestamp ``t_buy`` is followed
    by another BUY at ``t_buy2`` whose ``t_buy2 < t_buy +
    settlement_seconds`` AND the first position hasn't been closed yet.
    In a real binary market, capital is locked until resolution; a
    backtest that "reuses" the locked capital is implicitly assuming
    instant settlement.

For each detection, reports:

  - ``rule`` — the ``BL_NN`` code.
  - ``type`` — human-readable bias category (matches the list above).
  - ``severity`` — ``critical`` / ``warning`` / ``info``. ``critical``
    findings render the backtest *structurally unreliable* (look-ahead
    / leakage / hindsight); ``warning`` findings render it *probably
    unreliable but possibly explainable* (optimistic fills /
    survivorship); ``info`` is purely diagnostic.
  - ``evidence`` — concrete pointers to the offending data (which
    timestamps, which trade indices, which row count).
  - ``recommendation`` — how to fix (mirrors the W24-2 purge + embargo
    pattern, the realistic-execution pipeline in
    ``backtesting/engine.py`` etc.).

The detector is **pure-Python + synchronous** — no DB, no I/O, no
singleton state. Every detection method is a ``@staticmethod`` so a
caller can invoke just one rule (e.g. ``BiasDetector.detect_look_ahead_bias``
in a unit test) without instantiating the class. The class form is
provided so a caller can compose a custom rule subset via the
``rules=`` constructor argument.

Public surface:

  * :class:`BiasFinding` — single-finding dataclass.
  * :class:`BiasReport` — aggregate report (``findings`` +
    ``has_critical`` + ``critical_findings`` + ``summary``).
  * :class:`BiasDetector` — the detector itself.
  * :func:`bias_detector` — process-wide singleton (mirrors the
    ``oos_validator`` / ``drift_detector`` / ``calibrator`` pattern).
  * :func:`register_routes` — appends ``POST /api/backtest/bias-check``
    to a FastAPI app. The route returns the :meth:`BiasReport.to_dict`
    payload (``findings`` / ``summary`` / ``has_critical`` /
    ``critical_findings``) so a client can short-circuit promotion of
    an unreliable backtest without re-iterating the findings list.
    Same additive registration pattern as
    ``ml.out_of_sample.register_routes`` / ``ml.validation.register_routes``.

Companion files:

  * ``tests/test_bias_detector.py`` — full unit + API-route coverage.
  * ``api/server.py`` — calls ``register_routes(app)`` at module load
    so the route is live on the production FastAPI app.
  * ``backtesting/historical_replay.py`` — the
    :meth:`HistoricalReplayEngine.replay` method invokes
    :meth:`BiasDetector.analyze` after every backtest so a critical
    finding is logged at ``ERROR`` level (the backtest itself still
    returns — the caller decides whether to discard the result).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


# ── Severity vocabulary ────────────────────────────────────────────────────


SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_VALID_SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO)


# ── Rule metadata ──────────────────────────────────────────────────────────
#
# Each rule id (``BL_01``..``BL_10``) maps to a (short_name, default_severity)
# tuple so the detector / report can render the canonical type label even
# when a caller hands in a hand-rolled :class:`BiasFinding`.

_RULES: dict[str, dict[str, str]] = {
    "BL_01": {
        "type": "look_ahead_bias",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "Strip every feature whose as_of timestamp > decision_time. "
            "Re-derive features from a point-in-time store (e.g. "
            "ml.feature_store) so each row only sees data that existed "
            "at its decision timestamp."
        ),
    },
    "BL_02": {
        "type": "data_leakage",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "Re-partition the dataset so train and test index sets are "
            "disjoint. The ml.out_of_sample.OutOfSampleValidator already "
            "enforces this via its time-ordered sort + purge + embargo "
            "split — route the caller through it."
        ),
    },
    "BL_03": {
        "type": "optimistic_fills",
        "severity": SEVERITY_WARNING,
        "recommendation": (
            "Re-run the backtest through backtesting.engine.run_realistic_"
            "backtest — it crosses the spread (BUY pays the ask, SELL "
            "receives the bid), walks the book for partial fills, and "
            "applies a square-root market-impact term on top of the "
            "spread. Fills at period extremes are structurally impossible."
        ),
    },
    "BL_04": {
        "type": "future_information",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "Filter every feature column through a point-in-time join: "
            "feature.as_of <= decision_ts. A row that lacks a feature "
            "value as_of the decision time should be dropped (no forward-"
            "fill of future rows)."
        ),
    },
    "BL_05": {
        "type": "survivorship_bias",
        "severity": SEVERITY_WARNING,
        "recommendation": (
            "Re-pull the test universe from the FULL set of markets that "
            "existed at the train window's start time — including those "
            "that were later delisted / resolved NO. Track the original "
            "universe in a separate ``all_markets`` table so the test "
            "set's resolution distribution matches the population."
        ),
    },
    "BL_06": {
        "type": "selection_bias",
        "severity": SEVERITY_WARNING,
        "recommendation": (
            "Rename the strategy_id to a neutral label (no "
            "winners-only / best-performers / cherry-pick markers) and "
            "re-run on the full universe. A backtest whose name encodes "
            "a positive-selection signal cannot be trusted as a "
            "generalisation estimate."
        ),
    },
    "BL_07": {
        "type": "hindsight_filtering",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "Audit the strategy's generate_signal() — the >0.95 match "
            "rate between p_model and actual_outcome over >30 trades is "
            "only achievable if the signal peeks at the outcome. Re-"
            "implement the signal so it only consumes features whose "
            "as_of <= decision_ts."
        ),
    },
    "BL_08": {
        "type": "timestamp_leakage",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "Re-split with a purge gap between the train and test "
            "windows so max(train.timestamp) < min(test.timestamp). "
            "The ml.out_of_sample.OutOfSampleValidator's purge + "
            "embargo parameters implement exactly this guard."
        ),
    },
    "BL_09": {
        "type": "duplicate_participation",
        "severity": SEVERITY_CRITICAL,
        "recommendation": (
            "De-duplicate the underlying record set before splitting — "
            "two distinct indices referencing the same observation is a "
            "materialised-view / denormalised-join bug, not a split bug. "
            "Re-materialise features from the raw event log so each "
            "(token_id, decision_ts) pair appears exactly once."
        ),
    },
    "BL_10": {
        "type": "unrealistic_capital_reuse",
        "severity": SEVERITY_WARNING,
        "recommendation": (
            "Lock the capital deployed on each BUY until the position "
            "is closed (SELL or binary-market resolution). Track open "
            "positions in a positions list and subtract their notional "
            "from ``available_capital`` (not ``capital``) when sizing "
            "the next entry. See backtesting.historical_replay for the "
            "canonical implementation."
        ),
    },
}


# ── Findings + report containers ───────────────────────────────────────────


@dataclass
class BiasFinding:
    """One detected bias / leakage instance.

    Fields are intentionally JSON-serialisable (no ``datetime``, no
    ``numpy`` scalar) so the dataclass round-trips through
    ``json.dumps`` without a ``default=`` serializer — the API route
    in :func:`register_routes` returns ``dataclasses.asdict`` of a
    :class:`BiasReport` directly.
    """

    rule: str                       # ``BL_01``..``BL_10``
    type: str                        # human-readable bias category
    severity: str                    # ``critical`` / ``warning`` / ``info``
    evidence: str                    # concrete pointer to offending data
    recommendation: str              # how to fix
    detail: dict[str, Any] = field(default_factory=dict)
    # Optional machine-readable payload (offending indices, timestamps,
    # counts) so a caller can branch on the finding programmatically
    # without re-parsing the ``evidence`` string.

    def __post_init__(self) -> None:
        if self.rule not in _RULES:
            raise ValueError(
                f"Unknown bias rule {self.rule!r} — must be one of "
                f"{sorted(_RULES.keys())}"
            )
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}; "
                f"got {self.severity!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "type": self.type,
            "severity": self.severity,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "detail": dict(self.detail),
        }


@dataclass
class BiasReport:
    """Aggregate bias / leakage report.

    Built up by :meth:`BiasDetector.analyze` (one finding per rule that
    fires). The report exposes :attr:`has_critical` /
    :attr:`critical_findings` so a caller can short-circuit promotion of
    a backtest result without iterating the findings list.

    ``checked_at`` is the wall-clock timestamp at report construction
    time — surfaced in the API response so an operator can correlate
    the report with the X-Request-ID header.
    """

    findings: list[BiasFinding] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)
    # Summary metrics populated by ``BiasDetector.analyze`` —
    # counts by severity so the API response carries a single glanceable
    # payload alongside the per-finding list.
    n_critical: int = 0
    n_warning: int = 0
    n_info: int = 0
    n_total: int = 0

    @property
    def has_critical(self) -> bool:
        """True iff any finding has ``severity == 'critical'``."""
        return self.n_critical > 0

    @property
    def critical_findings(self) -> list[BiasFinding]:
        """Subset of :attr:`findings` with ``severity == 'critical'``."""
        return [f for f in self.findings if f.severity == SEVERITY_CRITICAL]

    @property
    def summary(self) -> dict[str, Any]:
        """One-glance summary for API / log surfaces."""
        return {
            "n_total": self.n_total,
            "n_critical": self.n_critical,
            "n_warning": self.n_warning,
            "n_info": self.n_info,
            "has_critical": self.has_critical,
            "checked_at": self.checked_at,
        }

    def add(self, finding: BiasFinding) -> None:
        """Append a finding and bump the per-severity counters."""
        self.findings.append(finding)
        self.n_total += 1
        if finding.severity == SEVERITY_CRITICAL:
            self.n_critical += 1
        elif finding.severity == SEVERITY_WARNING:
            self.n_warning += 1
        else:
            self.n_info += 1

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (used by the API route)."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
            "has_critical": self.has_critical,
            "critical_findings": [f.to_dict() for f in self.critical_findings],
        }


# ── Detector ───────────────────────────────────────────────────────────────


class BiasDetector:
    """Run the full bias / leakage rule set against a backtest payload.

    The detector is stateless — every method is a pure function of its
    inputs. The class form is provided so a caller can compose a custom
    rule subset via the ``rules=`` constructor argument (e.g. a unit
    test that only wants ``BL_01`` + ``BL_02``).
    """

    # Default settlement lag for the ``BL_10`` capital-reuse check (8 h,
    # the typical Polymarket binary-market resolution horizon). The
    # ``analyze()`` method accepts a per-call override so a caller
    # trading on minute-resolution markets can tighten the check.
    DEFAULT_SETTLEMENT_SECONDS: float = 8 * 3600.0

    # ── Constructor ──────────────────────────────────────────────────
    def __init__(self, rules: Sequence[str] | None = None) -> None:
        """
        Args:
            rules: Optional subset of ``BL_01``..``BL_10`` to run. When
                ``None`` (default) every rule is enabled. When a list
                is supplied, only those rules fire; this is the path
                a unit test takes to exercise one rule at a time.
        """
        if rules is None:
            self._rules: set[str] = set(_RULES.keys())
        else:
            unknown = [r for r in rules if r not in _RULES]
            if unknown:
                raise ValueError(
                    f"Unknown rule(s) {unknown!r} — must be one of "
                    f"{sorted(_RULES.keys())}"
                )
            self._rules = set(rules)

    # ── Individual detection rules ───────────────────────────────────

    @staticmethod
    def detect_look_ahead_bias(
        features: Sequence[Any] | None,
        timestamps: Sequence[float] | None,
        prediction_time: float,
        *,
        feature_as_of: Sequence[float] | None = None,
        tol: float = 1e-6,
    ) -> BiasFinding | None:
        """Check whether any feature timestamp > ``prediction_time``.

        Two call shapes:

          (1) ``detect_look_ahead_bias(features, timestamps, t)`` — the
              ``timestamps`` array is interpreted as the per-row
              as-of timestamp of each feature row. Any row whose
              timestamp exceeds ``prediction_time`` (by more than
              ``tol``) is a look-ahead violation.

          (2) ``detect_look_ahead_bias(features, timestamps, t,
              feature_as_of=...)`` — the ``feature_as_of`` array is
              the per-feature-column timestamp. ``timestamps`` is
              ignored in this case; only ``feature_as_of`` is checked.
              This shape mirrors the ``ml.feature_store`` schema where
              each feature value carries its own ``as_of`` timestamp.

        Returns ``None`` when no violation is found, otherwise a
        :class:`BiasFinding` with ``rule='BL_01'``.
        """
        if feature_as_of is not None:
            ts_arr = list(feature_as_of)
        elif timestamps is not None:
            ts_arr = list(timestamps)
        else:
            return None

        if not ts_arr:
            return None

        n_features = 0 if features is None else len(features)
        # If the caller passed both ``features`` and ``timestamps`` (shape 1),
        # they must be the same length — otherwise the timestamps don't
        # align with the feature rows and the check is meaningless.
        if feature_as_of is None and n_features > 0 and len(ts_arr) != n_features:
            return BiasFinding(
                rule="BL_01",
                type=_RULES["BL_01"]["type"],
                severity=SEVERITY_CRITICAL,
                evidence=(
                    f"feature matrix has {n_features} rows but timestamps "
                    f"array has {len(ts_arr)} — shape mismatch makes the "
                    f"look-ahead check ambiguous"
                ),
                recommendation=_RULES["BL_01"]["recommendation"],
                detail={
                    "n_features": n_features,
                    "n_timestamps": len(ts_arr),
                    "prediction_time": float(prediction_time),
                },
            )

        offenders = [
            (i, float(ts)) for i, ts in enumerate(ts_arr)
            if float(ts) > float(prediction_time) + tol
        ]
        if not offenders:
            return None

        first_idx, first_ts = offenders[0]
        return BiasFinding(
            rule="BL_01",
            type=_RULES["BL_01"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"{len(offenders)} of {len(ts_arr)} feature timestamps "
                f"exceed prediction_time={float(prediction_time):.3f} "
                f"(first offender: index {first_idx}, ts={first_ts:.3f})"
            ),
            recommendation=_RULES["BL_01"]["recommendation"],
            detail={
                "n_offenders": len(offenders),
                "n_total": len(ts_arr),
                "first_offender_index": first_idx,
                "first_offender_timestamp": first_ts,
                "prediction_time": float(prediction_time),
            },
        )

    @staticmethod
    def detect_data_leakage(
        train_indices: Iterable[Any] | None,
        test_indices: Iterable[Any] | None,
    ) -> BiasFinding | None:
        """Check for index-set overlap between train and test partitions.

        ``train_indices`` / ``test_indices`` are any iterables of
        hashable ids (Python ``int`` row indices, string token IDs,
        composite ``(timestamp, token_id)`` tuples — anything that
        supports ``set`` membership). Returns ``None`` when the
        partitions are disjoint, otherwise a :class:`BiasFinding` with
        ``rule='BL_02'`` listing the count and first 5 overlapping ids.
        """
        if train_indices is None or test_indices is None:
            return None
        train_set = set(train_indices)
        test_set = set(test_indices)
        if not train_set or not test_set:
            return None
        overlap = train_set & test_set
        if not overlap:
            return None
        sample = sorted(overlap, key=lambda x: str(x))[:5]
        return BiasFinding(
            rule="BL_02",
            type=_RULES["BL_02"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"{len(overlap)} indices appear in BOTH train and test "
                f"partitions (sample: {sample!r})"
            ),
            recommendation=_RULES["BL_02"]["recommendation"],
            detail={
                "n_overlap": len(overlap),
                "train_size": len(train_set),
                "test_size": len(test_set),
                "sample_overlap": list(sample),
            },
        )

    @staticmethod
    def detect_optimistic_fills(
        trades: Sequence[dict[str, Any]] | None,
        order_books: Sequence[dict[str, Any]] | None,
        *,
        tol: float = 1e-6,
    ) -> BiasFinding | None:
        """Check whether each trade's fill price was achievable.

        Iterates the ``trades`` list (each trade a dict with ``action``,
        ``price``, ``timestamp``, optional ``token_id``). For each trade,
        finds the matching order-book snapshot (same ``timestamp`` /
        ``token_id`` if the trade carries one; otherwise the snapshot
        at the same timestamp) and verifies the fill is inside the
        bid/ask spread:

          * BUY: ``fill_price`` must be ``>= best_bid`` (you can't buy
            below the bid in a CLOB without crossing the spread).
          * SELL: ``fill_price`` must be ``<= best_ask`` (symmetric).

        Also flags fills that match the period ``best_bid`` / ``best_ask``
        exactly (within ``tol``) — only achievable by a strategy that
        peeks at the book.

        Returns ``None`` when no violation is found. Otherwise returns
        a single :class:`BiasFinding` (``rule='BL_03'``) listing the
        count + first offender; the full per-trade evidence is in
        ``detail.offenders``.
        """
        if not trades:
            return None

        # Index order books by (timestamp, token_id) for O(1) lookup.
        # Falls back to a timestamp-only index when token_id is missing.
        books: dict[tuple[float, str | None], dict[str, Any]] = {}
        if order_books:
            for book in order_books:
                ts = float(book.get("timestamp", 0.0))
                tok = book.get("token_id")
                books[(ts, tok)] = book
                # Always also index under (ts, None) so a trade without
                # a token_id can still find the book by timestamp.
                books.setdefault((ts, None), book)

        if not books:
            # No order books supplied → can't verify fill achievability.
            # Don't fire a false positive; just return None.
            return None

        offenders: list[dict[str, Any]] = []
        for i, t in enumerate(trades):
            action = str(t.get("action", "")).upper()
            fill_price = float(t.get("price", t.get("fill_price", 0.0)) or 0.0)
            ts = float(t.get("timestamp", 0.0))
            tok = t.get("token_id")
            book = books.get((ts, tok)) or books.get((ts, None))
            if book is None:
                continue
            best_bid = float(book.get("best_bid", 0.0) or 0.0)
            best_ask = float(book.get("best_ask", 0.0) or 0.0)
            if best_bid <= 0.0 and best_ask <= 0.0:
                continue
            violation = None
            if action == "BUY" and fill_price < best_bid - tol:
                violation = (
                    f"BUY fill_price={fill_price:.6f} below "
                    f"best_bid={best_bid:.6f} (cannot buy below bid)"
                )
            elif action == "SELL" and fill_price > best_ask + tol:
                violation = (
                    f"SELL fill_price={fill_price:.6f} above "
                    f"best_ask={best_ask:.6f} (cannot sell above ask)"
                )
            elif action in ("BUY", "SELL"):
                # Exact-match check — fill equals the period extremum.
                if action == "BUY" and abs(fill_price - best_bid) < tol:
                    violation = (
                        f"BUY fill_price={fill_price:.6f} matches "
                        f"period best_bid exactly (suspicious — "
                        f"realistic fills include spread + impact)"
                    )
                elif action == "SELL" and abs(fill_price - best_ask) < tol:
                    violation = (
                        f"SELL fill_price={fill_price:.6f} matches "
                        f"period best_ask exactly (suspicious — "
                        f"realistic fills include spread + impact)"
                    )
            if violation:
                offenders.append({
                    "trade_index": i,
                    "action": action,
                    "fill_price": fill_price,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "timestamp": ts,
                    "token_id": tok,
                    "reason": violation,
                })

        if not offenders:
            return None

        first = offenders[0]
        return BiasFinding(
            rule="BL_03",
            type=_RULES["BL_03"]["type"],
            severity=SEVERITY_WARNING,
            evidence=(
                f"{len(offenders)} of {len(trades)} trades filled at "
                f"unrealistic prices (first offender: trade #{first['trade_index']}, "
                f"{first['reason']})"
            ),
            recommendation=_RULES["BL_03"]["recommendation"],
            detail={
                "n_offenders": len(offenders),
                "n_total_trades": len(trades),
                "offenders": offenders[:20],
            },
        )

    @staticmethod
    def detect_survivorship_bias(
        tested_markets: Iterable[Any] | None,
        all_markets: Iterable[Any] | None,
        *,
        min_tested_ratio: float = 0.50,
    ) -> BiasFinding | None:
        """Check whether only "winners" were tested.

        Compares the ``tested_markets`` set to the ``all_markets`` set.
        If the tested set is materially smaller than the universe
        (``len(tested) / len(all) < min_tested_ratio``), it's a sign
        that the backtest universe was curated (likely only markets
        that survived / resolved YES were kept) — i.e. survivorship
        bias.

        Returns ``None`` when no violation is found, otherwise a
        :class:`BiasFinding` (``rule='BL_05'``).
        """
        if tested_markets is None or all_markets is None:
            return None
        tested = set(tested_markets)
        all_set = set(all_markets)
        if not all_set:
            return None
        ratio = len(tested) / len(all_set) if all_set else 1.0
        if ratio >= min_tested_ratio:
            return None
        missing = all_set - tested
        sample = sorted(missing, key=lambda x: str(x))[:5]
        return BiasFinding(
            rule="BL_05",
            type=_RULES["BL_05"]["type"],
            severity=SEVERITY_WARNING,
            evidence=(
                f"only {len(tested)} of {len(all_set)} markets tested "
                f"(ratio={ratio:.2%} < threshold {min_tested_ratio:.0%}); "
                f"{len(missing)} markets missing from the test universe "
                f"(sample: {sample!r})"
            ),
            recommendation=_RULES["BL_05"]["recommendation"],
            detail={
                "n_tested": len(tested),
                "n_all": len(all_set),
                "n_missing": len(missing),
                "ratio": ratio,
                "threshold": min_tested_ratio,
                "sample_missing": list(sample),
            },
        )

    @staticmethod
    def detect_duplicate_participation(
        train_set: Iterable[Any] | None,
        test_set: Iterable[Any] | None,
    ) -> BiasFinding | None:
        """Check for the same records in both train and test sets.

        Subtly different from :meth:`detect_data_leakage`: this method
        compares the **record content** (each entry hashable as a
        ``tuple`` / ``str`` / ``int``) rather than the index. Catches
        the case where two distinct indices reference the same
        underlying observation (a materialised-view / denormalised-
        join bug).

        Returns ``None`` when the two sets have no record overlap,
        otherwise a :class:`BiasFinding` (``rule='BL_09'``).
        """
        if train_set is None or test_set is None:
            return None
        train_records = set(train_set)
        test_records = set(test_set)
        if not train_records or not test_records:
            return None
        overlap = train_records & test_records
        if not overlap:
            return None
        sample = sorted(overlap, key=lambda x: str(x))[:5]
        return BiasFinding(
            rule="BL_09",
            type=_RULES["BL_09"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"{len(overlap)} records appear in BOTH train and test "
                f"sets (sample: {sample!r})"
            ),
            recommendation=_RULES["BL_09"]["recommendation"],
            detail={
                "n_overlap": len(overlap),
                "train_size": len(train_records),
                "test_size": len(test_records),
                "sample_overlap": list(sample),
            },
        )

    @staticmethod
    def detect_future_information(
        features: Sequence[Sequence[float]] | None,
        feature_timestamps: Sequence[float] | None,
        decision_time: float,
        *,
        tol: float = 1e-6,
    ) -> BiasFinding | None:
        """Check whether any feature column's ``as_of`` timestamp
        exceeds ``decision_time``.

        Mirrors :meth:`detect_look_ahead_bias` but operates on a
        single-row feature vector + per-column ``as_of`` timestamps
        rather than a multi-row matrix + per-row timestamps. The two
        checks fire independently so a caller can detect either / both.

        Returns ``None`` when no violation is found, otherwise a
        :class:`BiasFinding` (``rule='BL_04'``).
        """
        if features is None or feature_timestamps is None:
            return None
        as_of = list(feature_timestamps)
        if not as_of:
            return None
        # Flatten the feature matrix into a single row vector if the
        # caller handed in a (1, F) matrix (common shape for a single
        # decision point).
        feature_row: Sequence[float] = []
        if features:
            first = features[0]
            if isinstance(first, (list, tuple)):
                feature_row = list(first)
            else:
                feature_row = list(features)
        if feature_row and len(feature_row) != len(as_of):
            return BiasFinding(
                rule="BL_04",
                type=_RULES["BL_04"]["type"],
                severity=SEVERITY_CRITICAL,
                evidence=(
                    f"feature vector has {len(feature_row)} columns but "
                    f"feature_timestamps has {len(as_of)} — shape mismatch"
                ),
                recommendation=_RULES["BL_04"]["recommendation"],
                detail={
                    "n_features": len(feature_row),
                    "n_timestamps": len(as_of),
                    "decision_time": float(decision_time),
                },
            )
        offenders = [
            (i, float(ts)) for i, ts in enumerate(as_of)
            if float(ts) > float(decision_time) + tol
        ]
        if not offenders:
            return None
        first_idx, first_ts = offenders[0]
        return BiasFinding(
            rule="BL_04",
            type=_RULES["BL_04"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"{len(offenders)} of {len(as_of)} feature columns have "
                f"as_of timestamps > decision_time={float(decision_time):.3f} "
                f"(first offender: column {first_idx}, as_of={first_ts:.3f})"
            ),
            recommendation=_RULES["BL_04"]["recommendation"],
            detail={
                "n_offenders": len(offenders),
                "n_total": len(as_of),
                "first_offender_column": first_idx,
                "first_offender_timestamp": first_ts,
                "decision_time": float(decision_time),
            },
        )

    @staticmethod
    def detect_selection_bias(
        strategy_id: str | None,
        *,
        markers: Sequence[str] | None = None,
    ) -> BiasFinding | None:
        """Check whether the ``strategy_id`` string itself encodes a
        positive-selection signal.

        Default markers (case-insensitive substring match against
        ``strategy_id``):

          * ``winner`` / ``winners_only``
          * ``best_performers`` / ``best_only``
          * ``cherry`` / ``cherrypick`` / ``cherry_pick``
          * ``curated`` / ``hand_picked``
          * ``top_n`` / ``topn``

        Returns ``None`` when no marker matches, otherwise a
        :class:`BiasFinding` (``rule='BL_06'``).
        """
        if not strategy_id:
            return None
        if markers is None:
            markers = (
                "winner", "best_performers", "best_only",
                "cherry", "cherrypick", "cherry_pick",
                "curated", "hand_picked", "top_n", "topn",
            )
        low = strategy_id.lower()
        matched = [m for m in markers if m in low]
        if not matched:
            return None
        return BiasFinding(
            rule="BL_06",
            type=_RULES["BL_06"]["type"],
            severity=SEVERITY_WARNING,
            evidence=(
                f"strategy_id={strategy_id!r} contains positive-selection "
                f"markers {matched!r} — suggests the backtest universe "
                f"was curated rather than sampled from the full population"
            ),
            recommendation=_RULES["BL_06"]["recommendation"],
            detail={
                "strategy_id": strategy_id,
                "matched_markers": list(matched),
            },
        )

    @staticmethod
    def detect_hindsight_filtering(
        predictions: Sequence[float] | None,
        outcomes: Sequence[float] | None,
        *,
        min_trades: int = 30,
        threshold: float = 0.95,
    ) -> BiasFinding | None:
        """Check whether the strategy's signals match the realized
        outcomes with suspiciously high precision.

        For each (prediction, outcome) pair where ``prediction > 0.5``
        (a YES bet) we check whether ``outcome == 1.0``; for
        ``prediction <= 0.5`` (a NO bet) we check whether ``outcome ==
        0.0``. The match rate over ``min_trades`` trades must be
        strictly below ``threshold``; otherwise the strategy is
        effectively peeking at the outcome.

        Returns ``None`` when the match rate is below threshold or
        the sample is too small, otherwise a :class:`BiasFinding`
        (``rule='BL_07'``).
        """
        if predictions is None or outcomes is None:
            return None
        n = min(len(predictions), len(outcomes))
        if n < min_trades:
            return None
        matches = 0
        for i in range(n):
            pred = float(predictions[i])
            actual = float(outcomes[i])
            bet = 1 if pred > 0.5 else 0
            if bet == int(round(actual)):
                matches += 1
        match_rate = matches / n if n > 0 else 0.0
        if match_rate <= threshold:
            return None
        return BiasFinding(
            rule="BL_07",
            type=_RULES["BL_07"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"signal/outcome match rate = {match_rate:.4f} over {n} "
                f"trades exceeds threshold {threshold:.2f} — strategy is "
                f"effectively peeking at the outcome"
            ),
            recommendation=_RULES["BL_07"]["recommendation"],
            detail={
                "n_trades": n,
                "n_matches": matches,
                "match_rate": match_rate,
                "threshold": threshold,
            },
        )

    @staticmethod
    def detect_timestamp_leakage(
        train_timestamps: Sequence[float] | None,
        test_timestamps: Sequence[float] | None,
        *,
        tol: float = 1e-6,
    ) -> BiasFinding | None:
        """Check whether train and test windows overlap in time.

        The train window's max timestamp must be strictly less than
        the test window's min timestamp (within ``tol``). Returns
        ``None`` when the windows are disjoint, otherwise a
        :class:`BiasFinding` (``rule='BL_08'``).
        """
        if not train_timestamps or not test_timestamps:
            return None
        train_ts = [float(t) for t in train_timestamps]
        test_ts = [float(t) for t in test_timestamps]
        train_max = max(train_ts)
        test_min = min(test_ts)
        if train_max < test_min - tol:
            return None
        # Compute overlap window for the evidence string.
        train_min = min(train_ts)
        test_max = max(test_ts)
        overlap_lo = max(train_min, test_min)
        overlap_hi = min(train_max, test_max)
        overlap_width = max(0.0, overlap_hi - overlap_lo)
        return BiasFinding(
            rule="BL_08",
            type=_RULES["BL_08"]["type"],
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"train window max timestamp {train_max:.3f} >= test window "
                f"min timestamp {test_min:.3f} — windows overlap by "
                f"{overlap_width:.3f}s (train=[{train_min:.3f}, {train_max:.3f}], "
                f"test=[{test_min:.3f}, {test_max:.3f}])"
            ),
            recommendation=_RULES["BL_08"]["recommendation"],
            detail={
                "train_min": train_min,
                "train_max": train_max,
                "test_min": test_min,
                "test_max": test_max,
                "overlap_width": overlap_width,
                "n_train": len(train_ts),
                "n_test": len(test_ts),
            },
        )

    @staticmethod
    def detect_unrealistic_capital_reuse(
        trades: Sequence[dict[str, Any]] | None,
        *,
        settlement_seconds: float = 8 * 3600.0,
    ) -> BiasFinding | None:
        """Check whether capital was reused before settlement.

        Walks the trade list in chronological order. Tracks each BUY
        trade's (timestamp, size) until it's closed by a matching SELL.
        If a second BUY arrives at ``t_buy2`` while a previous BUY is
        still open AND ``t_buy2 - t_buy1 < settlement_seconds``, the
        capital locked by the first BUY was "reused" before settlement
        — a backtest-only artefact.

        Returns ``None`` when no violation is found, otherwise a
        :class:`BiasFinding` (``rule='BL_10'``).
        """
        if not trades:
            return None
        # Sort by timestamp (stable on the original index so the
        # evidence string lists trades in chronological order).
        indexed = list(enumerate(trades))
        indexed.sort(key=lambda kv: float(kv[1].get("timestamp", 0.0)))

        open_buys: list[dict[str, Any]] = []
        offenders: list[dict[str, Any]] = []
        for orig_idx, t in indexed:
            action = str(t.get("action", "")).upper()
            ts = float(t.get("timestamp", 0.0))
            size = float(t.get("size", 0.0) or 0.0)
            if action == "BUY":
                # Check if any open BUY hasn't settled yet.
                for ob in open_buys:
                    if ts - ob["timestamp"] < settlement_seconds:
                        offenders.append({
                            "trade_index": orig_idx,
                            "action": "BUY",
                            "timestamp": ts,
                            "prior_buy_index": ob["trade_index"],
                            "prior_buy_timestamp": ob["timestamp"],
                            "gap_seconds": ts - ob["timestamp"],
                        })
                open_buys.append({
                    "trade_index": orig_idx,
                    "timestamp": ts,
                    "size": size,
                })
            elif action == "SELL":
                # Close one open BUY (FIFO).
                if open_buys:
                    open_buys.pop(0)

        if not offenders:
            return None
        first = offenders[0]
        return BiasFinding(
            rule="BL_10",
            type=_RULES["BL_10"]["type"],
            severity=SEVERITY_WARNING,
            evidence=(
                f"{len(offenders)} BUY trades entered while a prior BUY "
                f"was still within the {settlement_seconds:.0f}s settlement "
                f"window (first offender: trade #{first['trade_index']} "
                f"at ts={first['timestamp']:.3f}, prior BUY #{first['prior_buy_index']} "
                f"at ts={first['prior_buy_timestamp']:.3f}, gap={first['gap_seconds']:.1f}s)"
            ),
            recommendation=_RULES["BL_10"]["recommendation"],
            detail={
                "n_offenders": len(offenders),
                "n_total_trades": len(trades),
                "settlement_seconds": float(settlement_seconds),
                "offenders": offenders[:20],
            },
        )

    # ── Top-level analyze() ──────────────────────────────────────────

    def analyze(
        self,
        backtest_result: dict[str, Any] | Any,
        *,
        all_markets: Iterable[Any] | None = None,
        train_indices: Iterable[Any] | None = None,
        test_indices: Iterable[Any] | None = None,
        train_set: Iterable[Any] | None = None,
        test_set: Iterable[Any] | None = None,
        train_timestamps: Sequence[float] | None = None,
        test_timestamps: Sequence[float] | None = None,
        prediction_time: float | None = None,
        feature_as_of: Sequence[float] | None = None,
        decision_time: float | None = None,
        settlement_seconds: float | None = None,
    ) -> BiasReport:
        """Run every enabled rule against ``backtest_result``.

        ``backtest_result`` may be either a plain dict (the canonical
        shape returned by ``backtesting.engine.run_realistic_backtest``
        / ``backtesting.historical_replay.HistoricalReplayEngine.replay``
        via ``dataclasses.asdict``) or any object exposing a
        ``to_dict()`` method (covers ``BacktestResult`` /
        ``ReplayResult``). The method never raises — every rule is
        wrapped in its own try/except so a single malformed field
        doesn't blow up the whole report.

        Optional kwargs let a caller pass the auxiliary data the
        rules need (``all_markets`` for survivorship, ``train_indices``
        / ``test_indices`` for data leakage, ``train_timestamps`` /
        ``test_timestamps`` for timestamp leakage). When a rule lacks
        its required input, it's silently skipped (returns ``None``
        from its detection method).

        Returns a :class:`BiasReport` with one finding per rule that
        fired.
        """
        # Coerce ``backtest_result`` to a dict.
        if isinstance(backtest_result, dict):
            result = dict(backtest_result)
        elif hasattr(backtest_result, "to_dict"):
            result = dict(backtest_result.to_dict())
        elif backtest_result is None:
            result = {}
        else:
            # Last-resort: try to coerce via dict(). Falls back to {}
            # on failure so analyze() never raises on a malformed input.
            try:
                result = dict(backtest_result)
            except Exception:
                result = {}

        report = BiasReport()

        trades = result.get("trades") or []
        # Strategy-id check (BL_06).
        if "BL_06" in self._rules:
            strategy_id = (
                result.get("strategy_id")
                or result.get("strategy")
                or result.get("token_id")
            )
            finding = self.detect_selection_bias(strategy_id)
            if finding:
                report.add(finding)

        # Look-ahead bias (BL_01) — runs only when the caller supplied
        # explicit feature / timestamp inputs (the backtest result dict
        # alone doesn't carry per-row feature timestamps).
        if "BL_01" in self._rules and prediction_time is not None:
            features = result.get("features")
            timestamps = result.get("timestamps")
            finding = self.detect_look_ahead_bias(
                features, timestamps, prediction_time,
                feature_as_of=feature_as_of,
            )
            if finding:
                report.add(finding)

        # Data leakage (BL_02).
        if "BL_02" in self._rules:
            finding = self.detect_data_leakage(train_indices, test_indices)
            if finding:
                report.add(finding)

        # Optimistic fills (BL_03) — requires the per-trade order books
        # to be supplied (the historical_replay engine's trades carry
        # the fill price inline; the order_books kwarg is the matching
        # top-of-book snapshot per timestamp).
        if "BL_03" in self._rules:
            order_books = result.get("order_books") or result.get("snapshots")
            finding = self.detect_optimistic_fills(trades, order_books)
            if finding:
                report.add(finding)

        # Future information (BL_04) — requires a per-column feature
        # timestamp vector. ``decision_time`` is the per-decision
        # timestamp (defaults to ``prediction_time`` if omitted).
        if "BL_04" in self._rules and decision_time is not None:
            features = result.get("features")
            feature_ts = feature_as_of or result.get("feature_timestamps")
            finding = self.detect_future_information(
                features, feature_ts, decision_time,
            )
            if finding:
                report.add(finding)

        # Survivorship bias (BL_05).
        if "BL_05" in self._rules:
            tested = result.get("tested_markets")
            if tested is None and trades:
                # Heuristic: derive tested-markets set from the trades
                # list's ``token_id`` field when the caller didn't
                # supply ``all_markets`` explicitly.
                tested = {t.get("token_id") for t in trades if t.get("token_id")}
            finding = self.detect_survivorship_bias(tested, all_markets)
            if finding:
                report.add(finding)

        # Hindsight filtering (BL_07) — runs when the backtest result
        # carries per-trade ``p_model`` / ``actual_outcome`` fields
        # (the realistic-backtest engine shape) OR when the caller
        # supplies explicit ``predictions`` / ``outcomes`` lists.
        if "BL_07" in self._rules:
            predictions = result.get("predictions")
            outcomes = result.get("outcomes")
            if predictions is None or outcomes is None:
                # Try to derive from the trades list (the
                # run_realistic_backtest shape).
                if trades:
                    preds = [
                        float(t.get("p_model", 0.0)) for t in trades
                        if "p_model" in t
                    ]
                    outs = [
                        float(t.get("actual_outcome", 0.0)) for t in trades
                        if "actual_outcome" in t
                    ]
                    if len(preds) == len(outs) and len(preds) > 0:
                        predictions = preds
                        outcomes = outs
            finding = self.detect_hindsight_filtering(predictions, outcomes)
            if finding:
                report.add(finding)

        # Timestamp leakage (BL_08).
        if "BL_08" in self._rules:
            finding = self.detect_timestamp_leakage(
                train_timestamps, test_timestamps,
            )
            if finding:
                report.add(finding)

        # Duplicate participation (BL_09).
        if "BL_09" in self._rules:
            finding = self.detect_duplicate_participation(train_set, test_set)
            if finding:
                report.add(finding)

        # Unrealistic capital reuse (BL_10).
        if "BL_10" in self._rules:
            ss = (
                float(settlement_seconds)
                if settlement_seconds is not None
                else self.DEFAULT_SETTLEMENT_SECONDS
            )
            finding = self.detect_unrealistic_capital_reuse(
                trades, settlement_seconds=ss,
            )
            if finding:
                report.add(finding)

        if report.has_critical:
            logger.error(
                "CRITICAL bias detected in backtest: %s",
                [f.rule for f in report.critical_findings],
            )

        return report


# ── Singleton ──────────────────────────────────────────────────────────────


bias_detector = BiasDetector()


# ── HTTP surface ───────────────────────────────────────────────────────────


def register_routes(app: Any) -> None:
    """Append ``POST /api/backtest/bias-check`` to a FastAPI app.

    The endpoint accepts a JSON body of the shape::

        {
          "backtest_result": { ... },        # required — the backtest payload
          "all_markets": [...],               # optional — for BL_05
          "train_indices": [...],             # optional — for BL_02
          "test_indices": [...],              # optional — for BL_02
          "train_set": [...],                 # optional — for BL_09
          "test_set": [...],                  # optional — for BL_09
          "train_timestamps": [...],          # optional — for BL_08
          "test_timestamps": [...],           # optional — for BL_08
          "prediction_time": 1.0,             # optional — for BL_01
          "decision_time": 1.0,               # optional — for BL_04
          "feature_as_of": [...],            # optional — for BL_01 / BL_04
          "settlement_seconds": 28800         # optional — for BL_10
        }

    Only ``backtest_result`` is required; the optional fields let the
    caller opt into the rules that need auxiliary data not present in
    the backtest payload itself.

    Returns the :class:`BiasReport` payload (``findings`` /
    ``summary`` / ``has_critical`` / ``critical_findings``) so a client
    can short-circuit promotion of an unreliable backtest.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Same additive registration pattern as
    ``ml.out_of_sample.register_routes`` /
    ``ml.validation.register_routes``.
    """
    @app.post(
        "/api/backtest/bias-check",
        tags=["backtesting"],
        summary="Check a backtest result for bias and leakage",
        description=(
            "Runs the full W37-3 bias / leakage rule set "
            "(BL_01..BL_10) against the supplied backtest payload. "
            "Critical findings (look-ahead / data leakage / hindsight / "
            "timestamp leakage / duplicate participation) render the "
            "backtest structurally unreliable; warning findings "
            "(optimistic fills / survivorship / selection bias / "
            "capital reuse) flag issues that may be explainable but "
            "warrant review."
        ),
    )
    async def check_bias(payload: dict):
        """Check a backtest result for bias and leakage."""
        if not isinstance(payload, dict):
            raise _http_422("request body must be a JSON object")
        backtest_result = payload.get("backtest_result")
        if backtest_result is None:
            raise _http_422(
                "request body must contain a 'backtest_result' field "
                "(the backtest payload to analyse)"
            )

        # Filter the optional kwargs to only those that aren't None so
        # the detector's ``analyze()`` method receives clean ``None``
        # values for missing fields (the per-rule methods no-op on None).
        optional_kwargs: dict[str, Any] = {}
        for key in (
            "all_markets", "train_indices", "test_indices",
            "train_set", "test_set",
            "train_timestamps", "test_timestamps",
            "prediction_time", "decision_time", "feature_as_of",
            "settlement_seconds",
        ):
            if key in payload and payload[key] is not None:
                optional_kwargs[key] = payload[key]

        try:
            report = bias_detector.analyze(
                backtest_result=backtest_result,
                **optional_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — defensive last net
            logger.error(
                "[bias_detector] /api/backtest/bias-check failed: %s",
                exc, exc_info=True,
            )
            raise _http_500(
                "bias check failed — see server logs for details "
                "(correlate via the X-Request-ID response header)."
            )

        # Use ``to_dict()`` (not ``dataclasses.asdict``) so the
        # ``summary`` / ``has_critical`` / ``critical_findings``
        # @property fields are included in the response — ``asdict``
        # only serialises dataclass fields, not derived properties.
        return report.to_dict()


def _http_422(detail: str):
    """Build a FastAPI 422 (Unprocessable Entity) HTTPException lazily.

    Local import so the module is importable in non-server contexts
    (mirrors the lazy-import pattern in
    ``ml.out_of_sample.register_routes``).
    """
    from fastapi import HTTPException  # noqa: PLC0415
    return HTTPException(status_code=422, detail=detail)


def _http_500(detail: str):
    """Build a FastAPI 500 HTTPException lazily (no stack-trace leak)."""
    from fastapi import HTTPException  # noqa: PLC0415
    return HTTPException(status_code=500, detail=detail)


__all__ = [
    "BiasDetector",
    "BiasFinding",
    "BiasReport",
    "bias_detector",
    "register_routes",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
]
