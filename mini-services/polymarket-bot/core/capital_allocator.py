"""
core/capital_allocator.py — Sizing engine for new position entry.

Computes a USD position size for a candidate trade given the predicted
edge, model confidence, current drawdown from peak equity, existing
exposure on the same market / correlated group, and the available book
liquidity. The output is bounded by an institution-grade floor / cap
and gated by five safety conditions; if any gate trips the function
returns ``0.0`` (i.e. "do not trade").

Design
------
The allocator is intentionally **stateless and synchronous** — there is
no DB to consult, no singleton to reset between tests, no I/O. Every
input is supplied as a keyword-only argument so callers cannot
accidentally swap ``drawdown`` and ``existing_exposure``. The function
is pure: identical inputs always yield identical outputs, which makes
it trivially unit-testable and safe to call from both the hot scan loop
and the backtester.

Safety gates (evaluated in order; the first trip short-circuits to 0.0):

  1. ``edge <= 0``                  — no positive edge, nothing to size.
  2. ``confidence < MIN_CONFIDENCE`` — model is not confident enough to
                                       act on the predicted edge.
  3. ``drawdown > MAX_DRAWDOWN_USD`` — peak-to-trough drawdown has
                                       breached the hard MDD limit.
  4. ``existing_exposure > MAX_EXISTING_EXPOSURE_USD``
                                    — current open risk on the same
                                       market / correlated group already
                                       exceeds the per-group ceiling.
  5. ``liquidity <= 0``              — no book liquidity to absorb the
                                       order; submitting would move
                                       the market against us.

Saturating size curve
---------------------
When every gate passes, the raw size is computed as:

    raw = SIZE_SCALE * edge ** SIZE_CURVE_EXPONENT * confidence

The exponent ``SIZE_CURVE_EXPONENT = 0.4`` is **strictly sublinear**
(``exponent < 0.5``). This guarantees the institutional "4× edge gives
less than 2× size" saturation contract: scaling edge by 4 multiplies
the raw size by ``4 ** 0.4 ≈ 1.74`` — comfortably under the 2× ceiling.
A linear curve (exponent = 1.0) would multiply size by 4×; a square-root
curve (exponent = 0.5) would multiply by exactly 2×; only an exponent
strictly below 0.5 satisfies the ``< 2×`` contract for every valid
edge value (and therefore every raw size that doesn't already clip to
the floor or cap).

The raw size is then clipped to ``[MIN_SIZE_USD, MAX_SIZE_USD]`` =
``[$0.50, $3.00]`` so:

  - The minimum executable size is $0.50 (matches the
    ``max(0.5, ...)`` floor already used by ``strategies/signal_trader``
    after Kelly sizing).
  - The maximum per-trade size is $3.00 (matches
    ``risk.manager.MAX_POSITION_PER_MARKET`` so the allocator never
    suggests a size the risk gate would reject on the per-market cap).

The cap also bounds tail risk: even a 100 % edge with 100 % confidence
on a liquid, no-drawdown, no-existing-exposure market cannot push the
suggested size above $3.00 — exactly the institutional "size cap" the
task spec mandates.

Relationship to existing sizing code
-------------------------------------
``strategies/signal_trader._ml_signal`` currently computes ``size_usdc``
inline via a Kelly-fraction formula (``max(0.5, min($3, $100 * kelly_f))``).
This module is the **extracted, unit-testable core** of that sizing
step, with the per-market cap and floor preserved verbatim and three
extra gates (drawdown, existing exposure, liquidity) promoted from the
post-sizing risk check into the sizing decision itself — so a
non-viable trade returns 0 from the allocator rather than reaching
``check_order`` only to be rejected there.

T5 — Multiplier-based capital allocator
---------------------------------------
This module ALSO exposes a second, complementary sizing entry point —
:func:`allocate_capital` — which decouples signal generation from
capital sizing via a different (multiplier-based) design:

    raw_size = saturating_edge(edge)              # Michaelis-Menten curve
    size = raw_size
           * smoothstep(confidence)               # cubic-smooth confidence gate
           * calibration_mult(brier)              # isotonic-calibration health
           * drawdown_mult(drawdown_dollars)      # MDD-aware de-risking
           * correlation_mult(existing_exposure)  # per-market concentration gate
           * performance_mult(strategy_perf)      # realised-strategy scaling
           * liquidity_mult(liquidity_usdc)       # microstructure capacity
    → clamp to [0, MAX_POSITION_PER_MARKET] ($3)

T9 (:func:`allocate_size`) is the safety-gated BUY-side allocator used
by the hot scan loop; T5 (:func:`allocate_capital`) is the
attribution-friendly allocator that decomposes the size into named
multipliers for the API / dashboard. Both share the same ``$3`` cap.
``register_routes`` exposes the T5 allocator via
``GET /api/capital/allocation``.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── Safety-gate thresholds ────────────────────────────────────────────────
# Each constant doubles as the public contract surfaced to callers (the
# strategy layer, the risk gate, the API sizing endpoint) and as the
# value the unit tests pin. Renaming or re-typing any of these will break
# the T9 test suite — see ``tests/test_capital_allocator.py``.

#: Minimum model confidence required to act on a predicted edge. Below
#: this floor the allocator refuses to size (returns 0.0) regardless of
#: how large the edge is — a 50 % edge with 10 % confidence is not a
#: trade we want to take. Matches the ``signal_trader._min_confidence``
#: floor (which is itself ``max(0.45, settings.signal_min_confidence)``).
MIN_CONFIDENCE: float = 0.45

#: Hard drawdown ceiling. When ``peak_equity - current_equity`` exceeds
#: this dollar amount the allocator returns 0.0 — the MDD circuit breaker
#: in ``risk.manager`` will independently halt trading at the same
#: threshold, but checking it here avoids suggesting a size for an order
#: that would then be rejected. Matches ``risk.MAX_DRAWDOWN_LIMIT = $8``.
MAX_DRAWDOWN_USD: float = 8.0

#: Per-market / per-correlated-group exposure ceiling. When existing
#: open exposure on the same token (or its correlated group) already
#: exceeds this dollar amount the allocator returns 0.0 — prevents
#: doubling-down on a single thesis. Lower than
#: ``risk.MAX_CORRELATED_EXPOSURE = $8`` because the allocator is the
#: *first* gate: by the time the risk engine's $8 correlated-group cap
#: trips the position is already over-concentrated; sizing to zero at
#: $5 keeps us comfortably inside the institutional ceiling.
MAX_EXISTING_EXPOSURE_USD: float = 5.0

#: Maximum position size the allocator will ever suggest. Matches
#: ``risk.MAX_POSITION_PER_MARKET = $3`` exactly so the suggested size
#: always clears the risk engine's per-market cap gate (no redundant
#: rejection). Note the floor and cap together bound the output to the
#: half-open interval ``[$0.50, $3.00]`` for every non-zero return.
MAX_SIZE_USD: float = 3.0

#: Minimum executable position size. Below this the order is uneconomic
#: (Polymarket's minimum notional + gas + slippage would consume the
#: entire edge) so the allocator floors the raw size up to $0.50 —
#: matching the ``max(0.5, ...)`` idiom already in
#: ``strategies/signal_trader._ml_signal`` (line 333). The floor only
#: applies *after* every safety gate has passed; a gated trade returns
#: exactly ``0.0``, never ``0.50``.
MIN_SIZE_USD: float = 0.50

# ── Sizing curve parameters ───────────────────────────────────────────────
# Tuned so that typical Polymarket edges (1 %–20 %) and confidences
# (50 %–85 %) produce sizes spanning most of the [$0.50, $3.00] band:
#
#   edge=0.02, conf=0.50  → raw = 5.0 * 0.02^0.4 * 0.50 ≈ $0.44 → floor $0.50
#   edge=0.05, conf=0.70  → raw = 5.0 * 0.05^0.4 * 0.70 ≈ $1.04
#   edge=0.20, conf=0.80  → raw = 5.0 * 0.20^0.4 * 0.80 ≈ $2.10
#   edge=1.00, conf=1.00  → raw = 5.0 * 1.00^0.4 * 1.00 = $5.00 → cap $3.00
#
# The 4×-edge-< 2×-size saturation invariant is provable analytically:
# for any ``e > 0``, ``raw(4e) / raw(e) = 4 ** SIZE_CURVE_EXPONENT =
# 4 ** 0.4 ≈ 1.741 < 2``.

#: Linear scale on the raw size formula. ``SIZE_SCALE = 5.0`` places
#: typical edges in the middle of the [$0.50, $3.00] band so neither the
#: floor nor the cap dominates the curve in the operating regime.
SIZE_SCALE: float = 5.0

#: Sublinear exponent on ``edge`` in the raw size formula. Strictly
#: less than 0.5 so 4× edge yields strictly less than 2× raw size (the
#: "saturating" contract from the T9 task spec). ``0.4`` is chosen so
#: the saturation ratio is comfortably below 2 (≈ 1.74) — a small
#: margin to absorb float round-off without flipping the ``<`` test.
SIZE_CURVE_EXPONENT: float = 0.4


def allocate_size(
    *,
    edge: float,
    confidence: float,
    drawdown: float,
    existing_exposure: float,
    liquidity: float,
) -> float:
    """Compute the USD position size for a candidate trade.

    All arguments are keyword-only — swapping ``drawdown`` and
    ``existing_exposure`` would silently produce wrong results
    (both gates return 0 but for different reasons), so the
    keyword-only signature forces callers to be explicit.

    Parameters
    ----------
    edge:
        Predicted edge in absolute probability units (e.g. ``0.05``
        for a 5 % edge). Must be strictly positive for a non-zero
        return; ``edge <= 0`` short-circuits to ``0.0``.
    confidence:
        Model confidence in ``[0.0, 1.0]``. Must be ``>= 0.45`` for
        a non-zero return.
    drawdown:
        Current peak-to-trough drawdown in USD (``peak_equity -
        current_equity``). Must be ``<= 8.0`` for a non-zero return.
    existing_exposure:
        Current open exposure on the same market / correlated group
        in USD. Must be ``<= 5.0`` for a non-zero return.
    liquidity:
        Available book liquidity in USD (sum of top-N levels on the
        side we'd cross). Must be strictly positive (``> 0``) for
        a non-zero return; ``liquidity == 0`` short-circuits to
        ``0.0``.

    Returns
    -------
    float
        Suggested position size in USD. Always in ``[$0.50, $3.00]``
        when any non-zero size is suggested; exactly ``0.0`` when
        any safety gate trips.

    Notes
    -----
    The function is pure: identical inputs yield identical outputs
    and there are no side effects. Safe to call from the hot scan
    loop, the backtester, or a sizing-debug REPL without tainting
    any global state.
    """
    # ── Safety gates (evaluated in order; first trip → 0.0) ───────────────
    # Order matters only for the human-readable reason string a future
    # caller might attach; the return value is ``0.0`` regardless of
    # which gate tripped. The cheapest comparisons (no math) come first.

    # (1) Edge must be strictly positive. ``edge == 0`` means "no
    #     predicted advantage" — nothing to size. Negative edges (model
    #     disagrees with the market in the opposite direction) are
    #     sized by the symmetric SELL path, not by this BUY allocator.
    if edge <= 0.0:
        return 0.0

    # (2) Confidence floor. The model must be at least 45 % confident
    #     in its prediction before the edge is worth acting on — the
    #     same floor the strategy layer applies in
    #     ``signal_trader._ml_signal`` (line 275).
    if confidence < MIN_CONFIDENCE:
        return 0.0

    # (3) Drawdown ceiling. Past this point the institutional MDD
    #     breaker trips anyway; sizing to zero here avoids suggesting
    #     an order that the risk gate would reject one step later.
    if drawdown > MAX_DRAWDOWN_USD:
        return 0.0

    # (4) Existing-exposure ceiling. Doubling down on a single thesis
    #     past this point breaches concentration discipline even if
    #     the per-market cap ($3) hasn't tripped yet.
    if existing_exposure > MAX_EXISTING_EXPOSURE_USD:
        return 0.0

    # (5) Liquidity gate. Submitting into an empty book would cross
    #     the spread by the full remaining ask (or, worse, fail to
    #     fill at all). ``liquidity <= 0`` catches both the literal
    #     zero-liquidity case (no levels on our side) and any negative
    #     sentinel a buggy upstream might emit.
    if liquidity <= 0.0:
        return 0.0

    # ── Saturating size curve ────────────────────────────────────────────
    # ``edge ** 0.4`` is sublinear (exponent < 0.5) so 4× edge yields
    # ``4 ** 0.4 ≈ 1.74×`` raw size — comfortably under the 2× ceiling
    # mandated by the T9 task spec. ``confidence`` multiplies linearly
    # (no saturation on confidence — a 2× more confident model should
    # suggest a 2× larger size, all else equal).
    raw_size: float = SIZE_SCALE * (edge ** SIZE_CURVE_EXPONENT) * confidence

    # ── Floor + cap ──────────────────────────────────────────────────────
    # Apply the cap first then the floor; both bounds are inclusive.
    # ``max(MIN_SIZE_USD, min(MAX_SIZE_USD, raw))`` is equivalent to
    # ``sorted([MIN_SIZE_USD, raw, MAX_SIZE_USD])[1]`` but ~3× faster
    # and more obviously correct to a reader.
    if raw_size > MAX_SIZE_USD:
        return MAX_SIZE_USD
    if raw_size < MIN_SIZE_USD:
        return MIN_SIZE_USD
    return raw_size


# ──────────────────────────────────────────────────────────────────────────
# T5 — Multiplier-based capital allocator.
# ──────────────────────────────────────────────────────────────────────────
# A second, complementary sizing entry point that decouples signal
# generation from capital sizing via a multiplier-stack design:
#
#     raw_size = saturating_edge(edge)              # Michaelis-Menten curve
#     size = raw_size * smoothstep(confidence)
#                          * calibration_mult(brier)
#                          * drawdown_mult(drawdown_dollars)
#                          * correlation_mult(existing_exposure)
#                          * performance_mult(strategy_perf)
#                          * liquidity_mult(liquidity_usdc)
#     → clamp to [0, MAX_POSITION_PER_MARKET]
#
# The output is bounded by ``[0, $3.00]`` (no MIN_SIZE_USD floor — T5
# can return any value in the closed interval, including values below
# the $0.50 executable floor, because the downstream risk gate
# separately enforces the $0.50 minimum on submitted orders). T9's
# :func:`allocate_size` remains the safety-gated BUY-side allocator used
# by the hot scan loop; T5's :func:`allocate_capital` is the
# attribution-friendly allocator surfaced via the HTTP API for the
# dashboard / what-if analysis.
#
# Both entry points share the same hard ``$3`` cap (``MAX_SIZE_USD`` /
# ``MAX_POSITION_PER_MARKET``) and the same MDD ceiling
# (``MAX_DRAWDOWN_USD`` / ``MAX_DRAWDOWN_LIMIT``) — the T5 aliases point
# to the T9 constants so a future re-tune of the cap or the MDD limit
# propagates to both allocators atomically (no drift).

# ── T5 aliases onto the T9 constants (single source of truth) ───────────────
#: Per-market cap surfaced under the T5 name (alias of T9's ``MAX_SIZE_USD``).
#: Kept as a separate symbol so T5 callers don't need to import a T9-prefixed
#: constant; both names reference the same float.
MAX_POSITION_PER_MARKET: float = MAX_SIZE_USD

#: MDD circuit-breaker ceiling surfaced under the T5 name (alias of T9's
#: ``MAX_DRAWDOWN_USD``).
MAX_DRAWDOWN_LIMIT: float = MAX_DRAWDOWN_USD

# ── Michaelis-Menten edge curve ──────────────────────────────────────────────
#   raw_size(edge) = V_MAX * edge / (K_M + edge)
#     edge = 0      → 0
#     edge = K_M    → V_MAX / 2 = $1.50 (half-saturation)
#     edge = 2*K_M  → 2/3 * V_MAX = $2.00
#     edge → ∞      → V_MAX = $3.00 (asymptote)
# K_M = 0.05 means a 5 % alpha edge deploys half the per-market cap.
EDGE_V_MAX: float = MAX_POSITION_PER_MARKET
EDGE_K_M: float = 0.05

# ── Liquidity curve (Michaelis-Menten saturation against LIQUIDITY_K) ────────
#   m(liq) = liq / (K + liq)
#     liq = $0   → 0.0  (no depth, no allocation)
#     liq = $50  → 0.5
#     liq = $100 → 0.667
#     liq = $500 → 0.909
#     liq → ∞    → 1.0
LIQUIDITY_K: float = 50.0

# ── Calibration thresholds (mirror risk.manager.dynamic_model_risk_multiplier)
BRIER_HEALTHY: float = 0.16   # ≤ → 1.00
BRIER_MODERATE: float = 0.22   # ≤ → 0.60; > → 0.30


def smoothstep(t: float) -> float:
    """Hermite-cubic smoothstep on ``[0, 1]``: ``3t² - 2t³``.

    Returns ``0`` at ``t=0``, ``1`` at ``t=1``, with zero derivative at
    both endpoints (smooth fade in/out). Inputs outside ``[0, 1]`` are
    clamped. Used by :func:`confidence_mult` and
    :func:`correlation_mult` so the gate transitions are graceful
    rather than hard cutoffs.
    """
    t = max(0.0, min(1.0, float(t)))
    return 3.0 * t * t - 2.0 * t * t * t


def saturating_edge(edge: float) -> float:
    """Michaelis-Menten saturating edge curve in ``[0, EDGE_V_MAX]``.

    A negative or zero edge yields ``0`` (no allocation without alpha);
    the curve asymptotes to ``EDGE_V_MAX`` (``$3.00``) as ``edge → ∞``.
    """
    e = float(edge or 0.0)
    if e <= 0.0:
        return 0.0
    return EDGE_V_MAX * e / (EDGE_K_M + e)


def confidence_mult(confidence: float) -> float:
    """Smoothstep confidence multiplier in ``[0, 1]``.

    ``confidence = |P(YES) - 0.5| * 2`` (already in ``[0, 1]``). The
    smoothstep suppresses low-confidence signals (confidence ≤ 0.5 is
    heavily attenuated) without the discontinuity of a hard threshold.
    """
    return smoothstep(confidence)


def _read_brier() -> float | None:
    """Best-effort read of the live ML model's Brier score (None if unavailable)."""
    try:
        from ml.model import ml_model  # local import — module must load without sklearn
        return float(ml_model.brier_score)
    except Exception:
        return None


def calibration_mult(brier: float | None = None) -> float:
    """Isotonic-calibration health multiplier in ``{0.30, 0.60, 1.00}``.

    Reads ``ml_model.brier_score`` by default; pass ``brier`` to override
    (e.g. from the drift detector's rolling Brier). Thresholds mirror
    ``risk.manager.dynamic_model_risk_multiplier``:

    =============  =========  ========
    Brier band     trigger    capacity
    =============  =========  ========
    ≤ 0.16         healthy    1.00
    0.16 < b ≤ 0.22 moderate   0.60
    > 0.22         degraded   0.30
    =============  =========  ========

    Returns ``1.0`` (full capacity) when the model is unavailable so the
    allocator never blocks the trading pipeline on an ML import failure.
    """
    if brier is None:
        brier = _read_brier()
        if brier is None:
            return 1.0
    b = float(brier)
    if b > BRIER_MODERATE:
        return 0.30
    if b > BRIER_HEALTHY:
        return 0.60
    return 1.00


def drawdown_mult(drawdown_dollars: float) -> float:
    """Drawdown de-risking multiplier in ``[0, 1]``.

    Linear fade from ``1.0`` (no drawdown) to ``0.0`` at
    ``MAX_DRAWDOWN_LIMIT`` (``$8.00``). Beyond the cap the multiplier is
    hard-zeroed — the risk gate's MDD circuit breaker will already have
    tripped, so the allocator returns ``0`` too (defence in depth).
    """
    dd = max(0.0, float(drawdown_dollars or 0.0))
    if dd <= 0.0:
        return 1.0
    if dd >= MAX_DRAWDOWN_LIMIT:
        return 0.0
    return 1.0 - (dd / MAX_DRAWDOWN_LIMIT)


def correlation_mult(existing_exposure: float) -> float:
    """Per-market / correlated-group concentration multiplier in ``[0, 1]``.

    ``existing_exposure`` is the USD already deployed in the same market or
    correlated event group. At ``$0`` exposure the multiplier is ``1.0``;
    at the per-market cap (``$3.00``) it is ``0.0`` (no further capital to
    the same name). Smoothstep-faded so the transition is graceful, not
    a hard cutoff at the cap.
    """
    exp = max(0.0, float(existing_exposure or 0.0))
    if exp <= 0.0:
        return 1.0
    if exp >= MAX_POSITION_PER_MARKET:
        return 0.0
    return 1.0 - smoothstep(exp / MAX_POSITION_PER_MARKET)


def performance_mult(strategy_performance: dict | float | None) -> float:
    """Realised-strategy performance multiplier in ``[0.25, 1.50]``.

    Accepts either:

    - ``None`` / empty dict → neutral (1.0)
    - dict with optional ``win_rate`` (``0..1``) and ``sharpe`` (any real)
    - scalar in ``[0, 1]`` → treated as ``win_rate``

    Blended as ``0.6 * win_rate_mult + 0.4 * sharpe_mult``, then clamped:

    =========================  ===============  ========
    Component                  mapping          range
    =========================  ===============  ========
    ``win_rate`` (60 % weight) ``0.5 + wr``     [0.5, 1.5]
    ``sharpe``  (40 % weight)  ``1.0 + 0.1*s``  [0.5, 1.3]
    =========================  ===============  ========
    """
    if strategy_performance is None:
        return 1.0
    if isinstance(strategy_performance, dict):
        if not strategy_performance:
            return 1.0
        wr_raw = strategy_performance.get("win_rate")
        sharpe_raw = strategy_performance.get("sharpe")
    elif isinstance(strategy_performance, (int, float)):
        wr_raw = float(strategy_performance)
        sharpe_raw = None
    else:
        return 1.0

    # Win-rate component: 0.5 .. 1.5 around the 50 % pivot.
    if wr_raw is None:
        wr_mult = 1.0
    else:
        wr = max(0.0, min(1.0, float(wr_raw)))
        wr_mult = 0.5 + wr

    # Sharpe component: linear in sharpe, clamped to [0.5, 1.3].
    if sharpe_raw is None:
        sharpe_mult = 1.0
    else:
        try:
            sh = float(sharpe_raw)
            sharpe_mult = max(0.5, min(1.3, 1.0 + 0.1 * sh))
        except (TypeError, ValueError):
            sharpe_mult = 1.0

    blended = 0.6 * wr_mult + 0.4 * sharpe_mult
    return max(0.25, min(1.50, blended))


def liquidity_mult(liquidity_usdc: float) -> float:
    """Microstructure capacity multiplier in ``[0, 1)``.

    Michaelis-Menten saturation against ``LIQUIDITY_K`` (``$50``):
    ``$0`` → ``0.0`` (no depth, no allocation), asymptotes to ``1.0``.
    """
    liq = max(0.0, float(liquidity_usdc or 0.0))
    if liq <= 0.0:
        return 0.0
    return liq / (LIQUIDITY_K + liq)


def _compute_t5(
    edge: float,
    confidence: float,
    liquidity: float,
    existing_exposure: float,
    drawdown: float,
    strategy_performance: dict | float | None,
    brier: float | None,
) -> tuple[float, dict[str, float]]:
    """Shared sizing core for :func:`allocate_capital` /
    :func:`allocation_breakdown`. Returns ``(size, components)`` so both
    callers use byte-identical logic (no drift between the programmatic
    API and the HTTP endpoint's response)."""
    raw = saturating_edge(edge)
    c_mult = confidence_mult(confidence)
    cal_mult = calibration_mult(brier)
    dd_mult = drawdown_mult(drawdown)
    corr_mult = correlation_mult(existing_exposure)
    perf_mult = performance_mult(strategy_performance)
    liq_mult = liquidity_mult(liquidity)

    product = c_mult * cal_mult * dd_mult * corr_mult * perf_mult * liq_mult
    size = max(0.0, min(raw * product, MAX_POSITION_PER_MARKET))

    components = {
        "raw_size": round(raw, 4),
        "confidence_mult": round(c_mult, 4),
        "calibration_mult": round(cal_mult, 4),
        "drawdown_mult": round(dd_mult, 4),
        "correlation_mult": round(corr_mult, 4),
        "performance_mult": round(perf_mult, 4),
        "liquidity_mult": round(liq_mult, 4),
        "product_mult": round(product, 6),
    }
    return size, components


def allocate_capital(
    strategy: str,
    edge: float,
    confidence: float,
    liquidity: float,
    existing_exposure: float = 0.0,
    drawdown: float = 0.0,
    strategy_performance: dict | float | None = None,
) -> float:
    """Compute the USD position size in ``[0, MAX_POSITION_PER_MARKET]`` (``$3``)
    for a signal given the current portfolio context.

    Combines the saturating Michaelis-Menten edge curve with smoothstep /
    saturating multipliers for confidence, calibration, drawdown,
    correlation, performance, and liquidity. The product is clamped to the
    per-market cap so the allocator's output is always a valid order size
    for the downstream risk gate (``risk.manager.check_order``).

    Parameters
    ----------
    strategy:
        Strategy name (audit / log attribution only — does not affect
        sizing directly; per-strategy scaling happens via
        ``strategy_performance``).
    edge:
        Signed alpha edge between model probability and market price
        (decimal, e.g. ``0.05`` = +5 %).
    confidence:
        Model confidence ``|P(YES) - 0.5| * 2`` in ``[0, 1]``.
    liquidity:
        USD depth on the side being taken.
    existing_exposure:
        USD already deployed in the same market / correlated group.
    drawdown:
        USD drawdown from the high-water mark.
    strategy_performance:
        ``dict`` (``win_rate`` 0..1, ``sharpe`` any real), scalar win-rate
        in ``[0, 1]``, or ``None`` for neutral (1.0).

    Returns
    -------
    float
        USD position size in ``[0, MAX_POSITION_PER_MARKET]`` (``$3``).
        ``0`` when any multiplier collapses to zero (no edge, no
        liquidity, full drawdown, or already-at-cap existing exposure).
    """
    size, _ = _compute_t5(
        edge=edge,
        confidence=confidence,
        liquidity=liquidity,
        existing_exposure=existing_exposure,
        drawdown=drawdown,
        strategy_performance=strategy_performance,
        brier=None,  # auto-detect from the live ML model
    )
    size = round(size, 4)
    log.debug(
        "[capital_allocator] strategy=%s edge=%.4f conf=%.3f liq=%.1f exp=%.2f "
        "dd=%.2f perf=%r → size=$%.4f",
        strategy, edge, confidence, liquidity, existing_exposure,
        drawdown, strategy_performance, size,
    )
    return size


def allocation_breakdown(
    strategy: str,
    edge: float,
    confidence: float,
    liquidity: float,
    existing_exposure: float = 0.0,
    drawdown: float = 0.0,
    strategy_performance: dict | float | None = None,
    brier: float | None = None,
) -> dict[str, object]:
    """Re-compute :func:`allocate_capital` and return the full component
    breakdown.

    Used by ``GET /api/capital/allocation`` so callers can see WHY the
    allocator returned a given size (which multiplier dominated, what the
    raw edge-curve value was, etc.). ``brier`` overrides the
    auto-detected ML Brier for what-if analysis without touching the live
    model; when ``None`` (default) the breakdown's ``size_usd`` matches
    what :func:`allocate_capital` returns byte-for-byte.
    """
    size, components = _compute_t5(
        edge=edge,
        confidence=confidence,
        liquidity=liquidity,
        existing_exposure=existing_exposure,
        drawdown=drawdown,
        strategy_performance=strategy_performance,
        brier=brier,
    )
    return {
        "strategy": strategy,
        "edge": float(edge or 0.0),
        "confidence": float(confidence or 0.0),
        "liquidity_usd": float(liquidity or 0.0),
        "existing_exposure_usd": float(existing_exposure or 0.0),
        "drawdown_usd": float(drawdown or 0.0),
        "strategy_performance": strategy_performance,
        "brier_override": brier,
        "model_brier": _read_brier(),
        "size_usd": round(size, 4),
        "cap_usd": MAX_POSITION_PER_MARKET,
        "drawdown_limit_usd": MAX_DRAWDOWN_LIMIT,
        "edge_k_m": EDGE_K_M,
        "edge_v_max": EDGE_V_MAX,
        "liquidity_k": LIQUIDITY_K,
        "components": components,
    }


# ── FastAPI route registration (T5) ─────────────────────────────────────────

def register_routes(app: object) -> None:
    """Append the capital-allocation endpoint to a FastAPI app.

    Endpoint (auth-protected by the caller's existing middleware):

    ``GET /api/capital/allocation``
        Query params:

        ================  ===========  =========================================
        Param             Default      Description
        ================  ===========  =========================================
        ``strategy``      (required)   Strategy name (audit / attribution).
        ``edge``          (required)   Signed alpha edge (decimal, e.g. 0.05).
        ``confidence``    ``0.5``      Model confidence ``|P(YES)-0.5|*2``.
        ``liquidity``     ``0``        USD depth on the side being taken.
        ``existing_exposure`` ``0``    USD already in market / group.
        ``drawdown``      ``0``        USD drawdown from the high-water mark.
        ``win_rate``      (optional)   Strategy realised win-rate (``0..1``).
        ``sharpe``        (optional)   Strategy realised Sharpe ratio.
        ``brier``         (optional)   Override ML Brier for what-if (``0..1``).
        ================  ===========  =========================================

        Returns the full :func:`allocation_breakdown` dict so callers can
        see why the allocator returned a given size (component-level
        transparency).
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/capital/allocation", tags=["capital"])
    async def _capital_allocation(
        strategy: str = Query(
            ...,
            description="Strategy name (audit / attribution only — does not affect sizing).",
        ),
        edge: float = Query(
            ...,
            ge=-1.0,
            le=1.0,
            description=(
                "Signed alpha edge between model probability and market "
                "price (decimal, e.g. 0.05 = +5%)."
            ),
        ),
        confidence: float = Query(
            0.5,
            ge=0.0,
            le=1.0,
            description="Model confidence |P(YES)-0.5|*2 in [0, 1].",
        ),
        liquidity: float = Query(
            0.0,
            ge=0.0,
            description="USD depth on the side being taken.",
        ),
        existing_exposure: float = Query(
            0.0,
            ge=0.0,
            description="USD already deployed in the same market / correlated group.",
        ),
        drawdown: float = Query(
            0.0,
            ge=0.0,
            description="USD drawdown from the high-water mark.",
        ),
        win_rate: float | None = Query(
            None,
            ge=0.0,
            le=1.0,
            description="Strategy realised win-rate (optional; feeds performance_mult).",
        ),
        sharpe: float | None = Query(
            None,
            description="Strategy realised Sharpe ratio (optional; feeds performance_mult).",
        ),
        brier: float | None = Query(
            None,
            ge=0.0,
            le=1.0,
            description="Override ML Brier score for what-if analysis (optional).",
        ),
    ):
        """Return the USD allocation size + full component breakdown."""
        # Build the strategy_performance payload from query params — None when
        # neither win_rate nor sharpe was supplied so performance_mult falls
        # through to its neutral (1.0) default.
        if win_rate is None and sharpe is None:
            perf: dict[str, float] | None = None
        else:
            perf = {}
            if win_rate is not None:
                perf["win_rate"] = win_rate
            if sharpe is not None:
                perf["sharpe"] = sharpe
        return allocation_breakdown(
            strategy=strategy,
            edge=edge,
            confidence=confidence,
            liquidity=liquidity,
            existing_exposure=existing_exposure,
            drawdown=drawdown,
            strategy_performance=perf,
            brier=brier,
        )


__all__ = [
    # T9 — safety-gated sizing
    "MIN_CONFIDENCE",
    "MAX_DRAWDOWN_USD",
    "MAX_EXISTING_EXPOSURE_USD",
    "MAX_SIZE_USD",
    "MIN_SIZE_USD",
    "SIZE_SCALE",
    "SIZE_CURVE_EXPONENT",
    "allocate_size",
    # T5 — multiplier-based sizing
    "MAX_POSITION_PER_MARKET",
    "MAX_DRAWDOWN_LIMIT",
    "EDGE_V_MAX",
    "EDGE_K_M",
    "LIQUIDITY_K",
    "BRIER_HEALTHY",
    "BRIER_MODERATE",
    "smoothstep",
    "saturating_edge",
    "confidence_mult",
    "calibration_mult",
    "drawdown_mult",
    "correlation_mult",
    "performance_mult",
    "liquidity_mult",
    "allocate_capital",
    "allocation_breakdown",
    "register_routes",
]
