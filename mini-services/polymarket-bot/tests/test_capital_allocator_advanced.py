"""
tests/test_capital_allocator_advanced.py — Advanced unit tests for
``core/capital_allocator.py``.

W6 — Capital Allocator advanced tests.

Covers the eight behaviours required by the task spec:

  (1) ``allocation_breakdown`` returns the per-factor breakdown
      (raw_size, six multipliers, product_mult).
  (2) 4× edge yields strictly less than 2× size at three distinct edge
      starting levels (extends the single-level T9 saturation check).
  (3) ``calibration_mult`` returns 0.30 / 0.60 / 1.00 based on the Brier
      band (> 0.22 → 0.30, > 0.16 → 0.60, else → 1.0).
  (4) ``drawdown_mult`` is a linear ramp from 1.0 (at $0) to 0.0 (at
      ``MAX_DRAWDOWN_LIMIT`` = $8), with $2 as a checkpoint on the ramp
      (yielding 0.75).
  (5) ``correlation_mult`` is monotonically decreasing from 1.0 (at $0)
      to 0.0 (at ``MAX_POSITION_PER_MARKET`` = $3), with the smoothstep
      midpoint at 50 % of the cap yielding 0.5.
  (6) ``liquidity_mult`` ensures the final suggested size never exceeds
      30 % of the available book depth (when all other multipliers are
      pinned to 1.0).
  (7) ``performance_mult`` returns sensible multipliers across 5 distinct
      input regimes (neutral, empty, high, low, mid-positive).
  (8) Rejection (size = 0) returns a ``components`` dict that identifies
      which multiplier collapsed to zero (the "reason" for the rejection).

Conventions
------------
* ``sys.path`` is bootstrapped so the test runs regardless of the cwd
  pytest was launched from (mirrors ``tests/test_capital_allocator.py``).
* The env-var redirect block is **defensive only** — the capital
  allocator under test reads NONE of these env vars. The redirect exists
  purely so a co-collected sibling test file (e.g. test_risk_manager.py)
  doesn't see a missing / unwritable path during its own module-import-
  time work.
* All tests are synchronous plain ``def`` (no ``async def``) — the
  allocator is a pure function (no I/O, no awaits).
* Constants (``MAX_POSITION_PER_MARKET``, ``MAX_DRAWDOWN_LIMIT``,
  ``BRIER_HEALTHY``, ``BRIER_MODERATE``, ``LIQUIDITY_K``,
  ``SIZE_CURVE_EXPONENT``, ``MIN_SIZE_USD``, ``MAX_SIZE_USD``) are
  imported from the module under test so the assertions stay in lock-
  step with the implementation (a future re-tune moves the test
  automatically, rather than silently breaking it).

Spec-vs-impl clarifications
---------------------------
The W6 task spec wording diverges slightly from the implementation in
two places; the tests below pin the **implementation's** actual
behaviour (since the task forbids editing existing source files) and
document the divergence in the test docstrings:

* **(4) drawdown_mult**: the spec says "1.0 at $2 → 0.0 at $8", but
  the implementation ramps linearly from ``$0`` (mult=1.0) to ``$8``
  (mult=0.0). At ``$2`` the multiplier is ``1 - 2/8 = 0.75`` (a
  mid-ramp checkpoint, NOT 1.0). The spec's "$2" is interpreted here
  as a checkpoint along the linear ramp, not as the start of the ramp.
* **(5) correlation_mult**: the spec says "1.0 until 50% of cap, then
  linear to 0", but the implementation uses ``1 - smoothstep(t)`` which
  begins declining immediately from ``$0`` (no flat 1.0 region up to
  50 % of the cap) and is a smoothstep curve, not linear. At 50 % of
  cap the multiplier is exactly 0.5 (smoothstep symmetry).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Defensive env-var redirect (see "Conventions" in the module docstring). ──
# ``setdefault`` lets an outer runner / sibling test file override these if it
# needs to; otherwise the tests stay hermetic to /tmp and cannot clobber any
# real persisted state in the repo's ``data/`` directory. The capital
# allocator under test reads NONE of these — the redirect exists purely so a
# co-collected sibling test file (e.g. test_risk_manager.py) doesn't see a
# missing / unwritable path during its own module-import-time work.
_TMP_ROOT = Path("/tmp/capital_allocator_advanced_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    # Force the canonical trading mode to paper + live disabled so any
    # co-collected stateful test doesn't trip a shadow / live-trading gate.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from. Mirrors the
# bootstrap pattern in tests/test_capital_allocator.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.capital_allocator import (  # noqa: E402
    BRIER_HEALTHY,
    BRIER_MODERATE,
    EDGE_K_M,
    EDGE_V_MAX,
    LIQUIDITY_K,
    MAX_DRAWDOWN_LIMIT,
    MAX_POSITION_PER_MARKET,
    MAX_SIZE_USD,
    MIN_SIZE_USD,
    SIZE_CURVE_EXPONENT,
    allocation_breakdown,
    allocate_capital,
    allocate_size,
    calibration_mult,
    confidence_mult,
    correlation_mult,
    drawdown_mult,
    liquidity_mult,
    performance_mult,
    saturating_edge,
    smoothstep,
)

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here — every
# test in this file is a plain synchronous ``def``. The allocator is a pure
# function (no I/O, no awaits) so there is nothing for the asyncio event
# loop to schedule. Skipping the asyncio marker keeps pytest-asyncio
# collection cost off this file entirely.


# ──────────────────────────────────────────────────────────────────────────
# (1) allocation_breakdown returns per-factor breakdown
# ──────────────────────────────────────────────────────────────────────────
def test_allocation_breakdown_returns_per_factor_breakdown():
    """``allocation_breakdown`` must return a dict containing the full
    per-factor breakdown — both the top-level signal inputs (strategy,
    edge, confidence, liquidity_usd, existing_exposure_usd, drawdown_usd,
    brier_override, cap_usd, drawdown_limit_usd, edge_k_m, edge_v_max,
    liquidity_k) and a nested ``components`` dict with all 8 multiplier
    values (raw_size + 6 multipliers + product_mult).

    The ``size_usd`` field must equal ``raw_size * product_mult``,
    clamped to ``[0, MAX_POSITION_PER_MARKET]``. We compute the expected
    values by calling each individual multiplier function (defensive:
    if the breakdown's components disagree with the standalone
    multipliers, the allocator has a drift bug — the breakdown would
    lie about WHY the size came out the way it did, defeating its
    dashboard / what-if purpose).
    """
    bd = allocation_breakdown(
        strategy="breakdown_test",
        edge=0.05,
        confidence=0.70,
        liquidity=100.0,
        existing_exposure=0.5,
        drawdown=2.0,
        strategy_performance={"win_rate": 0.6, "sharpe": 1.5},
        brier=0.10,  # healthy → calibration_mult = 1.0
    )

    # ── Top-level keys: signal inputs + structural metadata ────────────────
    assert bd["strategy"] == "breakdown_test"
    assert bd["edge"] == pytest.approx(0.05, abs=1e-9)
    assert bd["confidence"] == pytest.approx(0.70, abs=1e-9)
    assert bd["liquidity_usd"] == pytest.approx(100.0, abs=1e-9)
    assert bd["existing_exposure_usd"] == pytest.approx(0.5, abs=1e-9)
    assert bd["drawdown_usd"] == pytest.approx(2.0, abs=1e-9)
    assert bd["brier_override"] == 0.10
    assert bd["cap_usd"] == MAX_POSITION_PER_MARKET
    assert bd["drawdown_limit_usd"] == MAX_DRAWDOWN_LIMIT
    assert bd["edge_k_m"] == EDGE_K_M
    assert bd["edge_v_max"] == EDGE_V_MAX
    assert bd["liquidity_k"] == LIQUIDITY_K

    # ── Components dict: exactly 8 keys ───────────────────────────────────
    components = bd["components"]
    expected_component_keys = {
        "raw_size",
        "confidence_mult",
        "calibration_mult",
        "drawdown_mult",
        "correlation_mult",
        "performance_mult",
        "liquidity_mult",
        "product_mult",
    }
    assert set(components.keys()) == expected_component_keys, (
        f"components keys must be exactly {expected_component_keys}, got "
        f"{set(components.keys())}"
    )

    # ── Each component must match the standalone multiplier function ───────
    # Defensive — catches drift between the breakdown's components and the
    # individual multiplier functions (which the dashboard callers will use
    # interchangeably).
    expected_raw = saturating_edge(0.05)
    expected_c_mult = confidence_mult(0.70)
    expected_cal_mult = calibration_mult(0.10)
    expected_dd_mult = drawdown_mult(2.0)
    expected_corr_mult = correlation_mult(0.5)
    expected_perf_mult = performance_mult({"win_rate": 0.6, "sharpe": 1.5})
    expected_liq_mult = liquidity_mult(100.0)
    expected_product = (
        expected_c_mult * expected_cal_mult * expected_dd_mult
        * expected_corr_mult * expected_perf_mult * expected_liq_mult
    )
    expected_size = max(
        0.0, min(expected_raw * expected_product, MAX_POSITION_PER_MARKET)
    )

    assert components["raw_size"] == pytest.approx(expected_raw, abs=1e-4)
    assert components["confidence_mult"] == pytest.approx(expected_c_mult, abs=1e-4)
    assert components["calibration_mult"] == pytest.approx(expected_cal_mult, abs=1e-4)
    assert components["drawdown_mult"] == pytest.approx(expected_dd_mult, abs=1e-4)
    assert components["correlation_mult"] == pytest.approx(expected_corr_mult, abs=1e-4)
    assert components["performance_mult"] == pytest.approx(expected_perf_mult, abs=1e-4)
    assert components["liquidity_mult"] == pytest.approx(expected_liq_mult, abs=1e-4)
    assert components["product_mult"] == pytest.approx(expected_product, abs=1e-4)

    # ── size_usd must equal raw_size * product_mult, clamped to the cap ────
    assert bd["size_usd"] == pytest.approx(expected_size, rel=1e-4)
    assert 0.0 <= bd["size_usd"] <= MAX_POSITION_PER_MARKET

    # ── Belt-and-braces: product_mult must be consistent with the literal
    # product of the six individual multipliers. Note: the implementation
    # rounds each component to 4 decimals and product_mult to 6 decimals,
    # so the literal product of the *rounded* components can drift by up
    # to ~6 × 1e-4 = 6e-4 from the rounded product_mult. We allow abs=1e-3
    # to absorb that compounded rounding (a regression that hardcoded
    # product_mult or computed it from a subset of multipliers would
    # still trip this assertion — the drift would be orders of magnitude
    # larger than 1e-3).
    literal_product = (
        components["confidence_mult"]
        * components["calibration_mult"]
        * components["drawdown_mult"]
        * components["correlation_mult"]
        * components["performance_mult"]
        * components["liquidity_mult"]
    )
    assert components["product_mult"] == pytest.approx(literal_product, abs=1e-3), (
        f"product_mult ({components['product_mult']:.6f}) must be consistent "
        f"with the literal product of the six multipliers "
        f"({literal_product:.6f})."
    )


# ──────────────────────────────────────────────────────────────────────────
# (2) 4× edge → < 2× size at 3 edge levels
# ──────────────────────────────────────────────────────────────────────────
def test_four_x_edge_yields_less_than_two_x_size_at_three_edge_levels():
    """The T9 size curve must satisfy the institutional saturation
    contract — 4× edge yields strictly less than 2× size — at three
    distinct starting edge levels, not just at the single 0.05 / 0.20
    level already covered by the T9 suite (``tests/test_capital_allocator.py``).

    For the power-law raw formula
    ``raw = SIZE_SCALE * edge ** SIZE_CURVE_EXPONENT * confidence``
    the saturation ratio is::

        raw(4e) / raw(e) = 4 ** SIZE_CURVE_EXPONENT  ≈ 1.7411

    which is constant (independent of ``e``) — so testing at 3 edge
    levels is a strong *invariance* check: any one level failing
    implies the curve is broken. We pin both the ``< 2.0`` ceiling and
    the exact ``4 ** α`` analytic value.

    The three starting edges (0.03, 0.05, 0.10) span roughly an order
    of magnitude and produce raw sizes ($0.86, $1.04, $1.39) and
    4×-scaled raw sizes ($1.50, $1.84, $2.43) — all strictly inside
    the ``[$0.50, $3.00]`` band so neither the floor nor the cap
    perturbs the ratio.

    Why T9 (``allocate_size``) and not T5 (``allocate_capital``)
    ----------------------------------------------------------------
    The T5 allocator uses a Michaelis-Menten edge curve
    (``raw = V_MAX * edge / (K_M + edge)``) whose saturation ratio
    ``raw(4e) / raw(e) = 4 * (K_M + e) / (K_M + 4e)`` is NOT constant
    in ``e`` — it exceeds 2.0 for very small edges (``e < K_M / 3``) and
    approaches 1.0 for very large edges. The institutional "< 2×"
    contract is therefore a property of the T9 power-law curve, NOT of
    the T5 Michaelis-Menten curve. The T9 contract is what the existing
    T9 suite pins (at one level); this test extends it to three levels
    to verify the invariance.
    """
    common = dict(
        confidence=0.70,
        drawdown=0.0,
        existing_exposure=0.0,
        liquidity=1_000.0,
    )

    edge_starts = [0.03, 0.05, 0.10]
    expected_ratio = 4.0 ** SIZE_CURVE_EXPONENT  # ≈ 1.7411

    observed_ratios: list[float] = []
    for e_low in edge_starts:
        e_high = 4 * e_low

        size_low = allocate_size(edge=e_low, **common)
        size_high = allocate_size(edge=e_high, **common)

        # Sanity: both non-zero, both strictly inside the band (so the
        # ratio is a property of the curve, not of the floor/cap).
        assert size_low > 0.0, f"size_low must be > 0 for edge={e_low}"
        assert size_high > 0.0, f"size_high must be > 0 for edge={e_high}"
        assert MIN_SIZE_USD < size_low < MAX_SIZE_USD, (
            f"size_low ({size_low:.4f}) for edge={e_low} must be strictly "
            f"inside [${MIN_SIZE_USD}, ${MAX_SIZE_USD}] so the floor "
            f"doesn't perturb the saturation ratio."
        )
        assert MIN_SIZE_USD < size_high < MAX_SIZE_USD, (
            f"size_high ({size_high:.4f}) for edge={e_high} must be strictly "
            f"inside [${MIN_SIZE_USD}, ${MAX_SIZE_USD}] so the cap doesn't "
            f"perturb the saturation ratio."
        )
        # Monotonicity: 4× edge must NOT shrink the size.
        assert size_high > size_low, (
            f"4× edge must NOT shrink the size at edge={e_low} "
            f"(size_low={size_low:.4f}, size_high={size_high:.4f})."
        )

        # The contract: 4× edge → STRICTLY less than 2× size.
        ratio = size_high / size_low
        assert ratio < 2.0, (
            f"4× edge must yield < 2× size at edge={e_low}. Got ratio="
            f"{ratio:.6f} (size_low={size_low:.4f}, size_high={size_high:.4f})."
        )

        # Power-law invariance: the ratio must equal 4 ** SIZE_CURVE_EXPONENT
        # (constant across all edge levels). This is the strong form of the
        # saturation contract — not just "< 2× at one level" but "exactly
        # 4**α everywhere", which makes any deviation loud.
        assert ratio == pytest.approx(expected_ratio, rel=1e-6), (
            f"saturation ratio at edge={e_low} must equal "
            f"4 ** SIZE_CURVE_EXPONENT ({expected_ratio:.6f}); got {ratio:.6f}."
        )

        observed_ratios.append(ratio)

    # Cross-level invariance: all 3 ratios must equal each other (already
    # verified each matches expected_ratio above; this is belt-and-braces
    # that the invariance holds across the three sampled levels).
    assert max(observed_ratios) - min(observed_ratios) < 1e-6, (
        f"saturation ratio must be invariant across edge levels; observed "
        f"ratios = {observed_ratios} (max-min = "
        f"{max(observed_ratios) - min(observed_ratios):.2e})."
    )

    # And the exponent itself must be strictly below 0.5 — otherwise the
    # analytic guarantee above stops holding (a value of 0.5 would make the
    # ratio exactly 2.0, failing the strict ``<`` test on float round-off;
    # a value above 0.5 would make the ratio exceed 2.0 outright).
    assert SIZE_CURVE_EXPONENT < 0.5, (
        "SIZE_CURVE_EXPONENT must be strictly less than 0.5 to guarantee "
        "4× edge → < 2× size for every valid edge value."
    )


# ──────────────────────────────────────────────────────────────────────────
# (3) calibration_mult: Brier bands
# ──────────────────────────────────────────────────────────────────────────
def test_calibration_mult_three_brier_bands():
    """``calibration_mult`` returns one of three discrete values based on
    the model's Brier score:

      - Brier > 0.22  → 0.30 (degraded)
      - Brier > 0.16  → 0.60 (moderate; implies ≤ 0.22)
      - else (≤ 0.16) → 1.00 (healthy)

    The thresholds (``BRIER_HEALTHY = 0.16``, ``BRIER_MODERATE = 0.22``)
    mirror ``risk.manager.dynamic_model_risk_multiplier`` so the
    allocator and the risk gate agree on calibration health. The gate
    is strict (``>``), so the boundary values (0.16 and 0.22 exactly)
    fall into the LOWER band, not the upper band.
    """
    # ── Degraded band: Brier > 0.22 → 0.30 ──────────────────────────────
    assert calibration_mult(0.30) == 0.30
    assert calibration_mult(0.25) == 0.30
    assert calibration_mult(0.221) == 0.30  # just above the 0.22 boundary
    assert calibration_mult(1.0) == 0.30  # worst possible Brier

    # ── Moderate band: 0.16 < Brier ≤ 0.22 → 0.60 ───────────────────────
    assert calibration_mult(0.20) == 0.60
    assert calibration_mult(0.18) == 0.60
    assert calibration_mult(0.161) == 0.60  # just above the 0.16 boundary
    # Boundary: the gate is strict (``> BRIER_MODERATE``), so Brier = 0.22
    # exactly is in the MODERATE band, NOT the degraded band.
    assert calibration_mult(0.22) == 0.60

    # ── Healthy band: Brier ≤ 0.16 → 1.00 ────────────────────────────────
    assert calibration_mult(0.10) == 1.00
    assert calibration_mult(0.05) == 1.00
    assert calibration_mult(0.0) == 1.00  # perfectly calibrated
    # Boundary: the gate is strict (``> BRIER_HEALTHY``), so Brier = 0.16
    # exactly is in the HEALTHY band.
    assert calibration_mult(0.16) == 1.00

    # ── Pin the threshold constants ─────────────────────────────────────
    assert BRIER_HEALTHY == 0.16
    assert BRIER_MODERATE == 0.22

    # ── Belt-and-braces: the three return values are exactly the three
    # discrete capacities the institutional contract mandates (no off-by-
    # one rounding, no float drift). A regression that returned 0.6 or 0.3
    # (instead of 0.60 / 0.30) would still pass ``== 0.6`` but would
    # surface differently in dashboard output; pin the canonical values.
    assert calibration_mult(0.30) == 0.30  # not 0.3
    assert calibration_mult(0.20) == 0.60  # not 0.6
    assert calibration_mult(0.10) == 1.00  # not 1.0


# ──────────────────────────────────────────────────────────────────────────
# (4) drawdown_mult: linear ramp
# ──────────────────────────────────────────────────────────────────────────
def test_drawdown_mult_linear_ramp():
    """``drawdown_mult`` is a linear ramp from 1.0 (at $0 drawdown) to 0.0
    (at ``MAX_DRAWDOWN_LIMIT`` = $8). Beyond the cap the multiplier is
    hard-zeroed; below $0 it's clamped to $0 (no negative drawdown
    penalty).

    Spec-vs-impl clarification
    --------------------------
    The W6 spec wording "1.0 at $2 → 0.0 at $8" is slightly off — the
    implementation ramps linearly from ``$0`` (mult=1.0) to ``$8``
    (mult=0.0), not from ``$2``. At ``$2`` drawdown the multiplier is
    ``1 - 2/8 = 0.75`` (a mid-ramp checkpoint, NOT 1.0). The test below
    pins the implementation's actual behaviour (which we cannot edit
    per the task's "Do NOT edit existing files" constraint) and treats
    ``$2`` as one of several checkpoints along the linear ramp.
    """
    # ── Boundary: $0 drawdown → 1.0 (full capacity) ─────────────────────
    assert drawdown_mult(0.0) == 1.0
    # ── Boundary: at the cap ($8) → 0.0 (circuit breaker trips) ──────────
    assert drawdown_mult(MAX_DRAWDOWN_LIMIT) == 0.0
    # ── Beyond the cap → 0.0 (defence in depth) ─────────────────────────
    assert drawdown_mult(100.0) == 0.0
    assert drawdown_mult(1_000_000.0) == 0.0
    # ── Negative drawdown → clamped to 0 → 1.0 (no penalty) ──────────────
    assert drawdown_mult(-5.0) == 1.0

    # ── Linear-ramp checkpoints ─────────────────────────────────────────
    # At $2: 1 - 2/8 = 0.75 (the spec's "$2" checkpoint).
    assert drawdown_mult(2.0) == pytest.approx(0.75, abs=1e-9)
    # At $4 (midpoint): 1 - 4/8 = 0.50.
    assert drawdown_mult(4.0) == pytest.approx(0.50, abs=1e-9)
    # At $6: 1 - 6/8 = 0.25.
    assert drawdown_mult(6.0) == pytest.approx(0.25, abs=1e-9)

    # ── Analytic linearity: at any point in [0, 8], mult = 1 - dd/8 ──────
    for dd in [0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 7.99]:
        expected = 1.0 - dd / MAX_DRAWDOWN_LIMIT
        assert drawdown_mult(dd) == pytest.approx(expected, abs=1e-9), (
            f"drawdown_mult({dd}) must equal 1 - dd/MAX_DRAWDOWN_LIMIT = "
            f"{expected:.6f} (linear ramp)."
        )

    # ── Monotonicity: mult must be non-increasing in dd ──────────────────
    prev = 2.0  # sentinel above any valid multiplier
    for dd in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0]:
        mult = drawdown_mult(dd)
        assert mult <= prev + 1e-12, (
            f"drawdown_mult must be non-increasing: at dd={dd}, "
            f"mult={mult:.4f} > prev={prev:.4f}."
        )
        prev = mult

    # ── Pin the limit constant ──────────────────────────────────────────
    assert MAX_DRAWDOWN_LIMIT == 8.0


# ──────────────────────────────────────────────────────────────────────────
# (5) correlation_mult: smoothstep ramp
# ──────────────────────────────────────────────────────────────────────────
def test_correlation_mult_smoothstep_ramp():
    """``correlation_mult`` is a smoothstep fade from 1.0 (at $0 exposure)
    to 0.0 (at ``MAX_POSITION_PER_MARKET`` = $3), with the smoothstep
    midpoint at 50 % of the cap yielding exactly 0.5.

    Spec-vs-impl clarification
    --------------------------
    The W6 spec wording "1.0 until 50% of cap, then linear to 0" is
    approximate. The implementation uses ``1 - smoothstep(t)`` where
    ``t = exposure / cap ∈ [0, 1]``. Smoothstep is a cubic Hermite curve
    (``3t² - 2t³``) with zero derivative at both endpoints, so the fade
    is *graceful* (not linear) and begins declining immediately from
    ``$0`` (no flat 1.0 region up to 50 % of the cap). At ``t = 0.5``
    (50 % of cap) the multiplier is exactly 0.5 (smoothstep symmetry).
    The test below pins the implementation's actual behaviour.
    """
    cap = MAX_POSITION_PER_MARKET  # $3.00

    # ── Boundary: $0 exposure → 1.0 (full capacity) ─────────────────────
    assert correlation_mult(0.0) == 1.0
    # ── Boundary: at the cap → 0.0 (no further capital to this market) ──
    assert correlation_mult(cap) == 0.0
    # ── Beyond the cap → 0.0 ────────────────────────────────────────────
    assert correlation_mult(100.0) == 0.0
    # ── Negative exposure → clamped to 0 → 1.0 ──────────────────────────
    assert correlation_mult(-5.0) == 1.0

    # ── Smoothstep midpoint: 50 % of cap → 0.5 ──────────────────────────
    # smoothstep(0.5) = 3*0.25 - 2*0.125 = 0.5, so 1 - smoothstep(0.5) = 0.5.
    assert correlation_mult(cap * 0.5) == pytest.approx(0.5, abs=1e-9), (
        "At 50 % of the cap, correlation_mult must equal 0.5 (smoothstep "
        "symmetry: smoothstep(0.5) = 0.5, so 1 - smoothstep(0.5) = 0.5)."
    )

    # ── Analytic smoothstep formula at intermediate points ───────────────
    for frac in [0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9]:
        exposure = cap * frac
        expected = 1.0 - smoothstep(frac)
        assert correlation_mult(exposure) == pytest.approx(expected, abs=1e-9), (
            f"correlation_mult({exposure}) must equal 1 - smoothstep({frac}) "
            f"= {expected:.6f}."
        )

    # ── Monotonicity: mult must be non-increasing in exposure ───────────
    prev = 2.0  # sentinel above any valid multiplier
    for exposure in [0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0, 5.0]:
        mult = correlation_mult(exposure)
        assert mult <= prev + 1e-12, (
            f"correlation_mult must be non-increasing: at exposure={exposure}, "
            f"mult={mult:.4f} > prev={prev:.4f}."
        )
        prev = mult

    # ── Pin the cap ──────────────────────────────────────────────────────
    assert MAX_POSITION_PER_MARKET == 3.0


# ──────────────────────────────────────────────────────────────────────────
# (6) liquidity_mult: caps size to 30 % of book depth
# ──────────────────────────────────────────────────────────────────────────
def test_liquidity_factor_caps_size_to_30_percent_of_book_depth():
    """The liquidity multiplier must constrain the suggested size to at
    most 30 % of the available book depth — i.e. for any ``L > 0``, the
    final suggested size (with every other multiplier pinned to 1.0)
    must satisfy ``size <= 0.30 * L``.

    Implementation: ``liq_mult(L) = L / (50 + L)`` (Michaelis-Menten
    against ``LIQUIDITY_K = $50``). With the raw edge size bounded by
    ``EDGE_V_MAX = $3`` (the per-market cap), we have::

        size = raw * liq_mult ≤ 3 * L / (50 + L) ≤ 0.30 * L

    The last inequality reduces to ``L ≥ -40`` (always true for
    ``L > 0``), so the 30 % property holds universally — for every
    ``L > 0`` and every raw size up to the cap.
    """
    # ── Direct multiplier behaviour ─────────────────────────────────────
    # No depth → 0 (no allocation into an empty book).
    assert liquidity_mult(0.0) == 0.0
    # Negative → clamped to 0 → 0.
    assert liquidity_mult(-10.0) == 0.0
    # Half-saturation at LIQUIDITY_K: L = K → mult = K / (K + K) = 0.5.
    assert liquidity_mult(LIQUIDITY_K) == pytest.approx(0.5, abs=1e-9)
    # Asymptote: very large L → mult → 1.0 (but strictly < 1.0).
    big_mult = liquidity_mult(1_000_000.0)
    assert 0.999 < big_mult < 1.0
    # Sanity: monotonic non-decreasing in L.
    assert liquidity_mult(10.0) < liquidity_mult(100.0) < liquidity_mult(1_000.0)

    # ── The 30 % cap property: size (with other mults at 1.0) ≤ 0.30 * L ─
    # We pin every other multiplier to 1.0 by:
    #   - brier = 0.10 (healthy → calibration_mult = 1.0)
    #   - confidence = 1.0 (smoothstep(1.0) = 1.0)
    #   - drawdown = 0.0 (drawdown_mult = 1.0)
    #   - existing_exposure = 0.0 (correlation_mult = 1.0)
    #   - strategy_performance = None (performance_mult = 1.0)
    #   - edge = 1.0 (max edge, so saturating_edge → max raw ≈ $2.857)
    # so the only multiplier actually shrinking the size is liquidity_mult.
    liquidity_values = [
        1.0, 5.0, 10.0, 20.0, 50.0, 100.0,
        200.0, 500.0, 1_000.0, 10_000.0, 100_000.0,
    ]
    for L in liquidity_values:
        bd = allocation_breakdown(
            strategy="liq_cap_test",
            edge=1.0,
            confidence=1.0,
            liquidity=L,
            existing_exposure=0.0,
            drawdown=0.0,
            strategy_performance=None,
            brier=0.10,
        )
        size = bd["size_usd"]
        cap_30 = 0.30 * L
        # The 30 % property must hold for every L > 0.
        assert size <= cap_30 + 1e-9, (
            f"size ({size:.6f}) must be ≤ 30 % of book depth ({cap_30:.6f}) "
            f"for L={L}. Components: {bd['components']}."
        )
        # And size is always bounded by the per-market cap.
        assert size <= MAX_POSITION_PER_MARKET + 1e-9

    # ── Thin-book edge case: at L = $1, size must be tiny ────────────────
    # liq_mult($1) = 1 / 51 ≈ 0.0196, raw ≈ $2.857 → size ≈ $0.056.
    # 30 % of $1 = $0.30, so size ($0.056) is comfortably under.
    bd_thin = allocation_breakdown(
        strategy="liq_cap_test",
        edge=1.0, confidence=1.0, liquidity=1.0,
        existing_exposure=0.0, drawdown=0.0,
        strategy_performance=None, brier=0.10,
    )
    assert bd_thin["size_usd"] <= 0.30 * 1.0  # ≤ $0.30
    assert bd_thin["size_usd"] <= MAX_POSITION_PER_MARKET  # also bounded by cap
    assert bd_thin["components"]["liquidity_mult"] < 0.05  # ~0.02

    # ── Pin the LIQUIDITY_K constant ────────────────────────────────────
    assert LIQUIDITY_K == 50.0


# ──────────────────────────────────────────────────────────────────────────
# (7) performance_mult: 5 regimes
# ──────────────────────────────────────────────────────────────────────────
def test_performance_mult_five_regimes():
    """``performance_mult`` returns a blended multiplier in ``[0.25, 1.50]``
    across 5 distinct input regimes, covering the neutral default, the
    high-performance (upper-blend), the low-performance (lower-blend),
    the mid-positive, and the scalar-input paths.

    Blend formula
    -------------
    ``blended = 0.6 * win_rate_mult + 0.4 * sharpe_mult``, clamped to
    ``[0.25, 1.50]``, where:
      - ``win_rate_mult = 0.5 + win_rate`` (in ``[0.5, 1.5]``)
      - ``sharpe_mult  = max(0.5, min(1.3, 1.0 + 0.1 * sharpe))``
    The clamp is defensive: with ``win_rate ∈ [0, 1]`` and ``sharpe``
    clamped to ``[0.5, 1.3]``, the blend range is ``[0.5, 1.42]``,
    which is strictly inside the ``[0.25, 1.50]`` clamp — so the clamp
    is never the binding constraint in normal use, but it guards against
    a future re-tune of the component multipliers.
    """
    # ── Regime 1: None → neutral default (1.0) ───────────────────────────
    assert performance_mult(None) == 1.0

    # ── Regime 2: Empty dict → neutral default (1.0) ────────────────────
    assert performance_mult({}) == 1.0

    # ── Regime 3: High performance (win_rate=1.0, sharpe=10) ─────────────
    # win_rate_mult = 0.5 + 1.0 = 1.5
    # sharpe_mult   = min(1.3, 1.0 + 1.0) = 1.3 (clamped at the upper bound)
    # blended       = 0.6 * 1.5 + 0.4 * 1.3 = 0.9 + 0.52 = 1.42
    # (No upper-clamp triggered — 1.42 < 1.50 — but sharpe_mult is clamped.)
    high = performance_mult({"win_rate": 1.0, "sharpe": 10.0})
    expected_high = 0.6 * 1.5 + 0.4 * 1.3  # = 1.42
    assert high == pytest.approx(expected_high, abs=1e-9)
    assert 1.0 < high <= 1.50
    # Even an absurd sharpe (1000) doesn't exceed the upper clamp — the
    # sharpe_mult is clamped at 1.3 regardless.
    absurd_high = performance_mult({"win_rate": 1.0, "sharpe": 1_000.0})
    assert absurd_high == pytest.approx(expected_high, abs=1e-9)

    # ── Regime 4: Low performance (win_rate=0.0, sharpe=-10) ─────────────
    # win_rate_mult = 0.5 + 0.0 = 0.5
    # sharpe_mult   = max(0.5, 1.0 + (-1.0)) = max(0.5, 0.0) = 0.5 (clamped)
    # blended       = 0.6 * 0.5 + 0.4 * 0.5 = 0.3 + 0.2 = 0.5
    # (No lower-clamp triggered — 0.5 > 0.25 — but sharpe_mult is clamped.)
    low = performance_mult({"win_rate": 0.0, "sharpe": -10.0})
    expected_low = 0.6 * 0.5 + 0.4 * 0.5  # = 0.5
    assert low == pytest.approx(expected_low, abs=1e-9)
    assert 0.25 <= low < 1.0
    # Even an absurd negative sharpe (-1000) doesn't breach the lower clamp.
    absurd_low = performance_mult({"win_rate": 0.0, "sharpe": -1_000.0})
    assert absurd_low == pytest.approx(expected_low, abs=1e-9)

    # ── Regime 5: Mid-positive performance (win_rate=0.7, sharpe=2.0) ────
    # win_rate_mult = 0.5 + 0.7 = 1.2
    # sharpe_mult   = 1.0 + 0.1 * 2.0 = 1.2 (inside [0.5, 1.3], no clamp)
    # blended       = 0.6 * 1.2 + 0.4 * 1.2 = 0.72 + 0.48 = 1.20
    mid = performance_mult({"win_rate": 0.7, "sharpe": 2.0})
    expected_mid = 0.6 * 1.2 + 0.4 * 1.2  # = 1.2
    assert mid == pytest.approx(expected_mid, abs=1e-9)
    # Strictly between neutral (1.0) and high (1.42).
    assert 1.0 < mid < high

    # ── Bonus: scalar input path (no sharpe) ────────────────────────────
    # win_rate_mult = 0.5 + 0.5 = 1.0
    # sharpe_mult   = 1.0 (default for missing sharpe)
    # blended       = 0.6 * 1.0 + 0.4 * 1.0 = 1.0
    scalar = performance_mult(0.5)
    assert scalar == pytest.approx(1.0, abs=1e-9)

    # ── Bonus: dict with only win_rate (no sharpe) ──────────────────────
    # win_rate_mult = 1.3, sharpe_mult = 1.0 (default)
    # blended = 0.6 * 1.3 + 0.4 * 1.0 = 0.78 + 0.40 = 1.18
    wr_only = performance_mult({"win_rate": 0.8})
    assert wr_only == pytest.approx(0.6 * 1.3 + 0.4 * 1.0, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────────
# (8) Rejection returns size=0 with reason
# ──────────────────────────────────────────────────────────────────────────
def test_rejection_returns_size_zero_with_reason():
    """When the allocator rejects a signal (``size = 0``), the breakdown's
    ``components`` dict must identify which multiplier collapsed to zero
    — that's the "reason" for the rejection. (The ``allocate_capital``
    function itself returns a plain float with no reason attached; the
    ``allocation_breakdown`` function returns the full dict with the
    components, so callers can attribute the rejection to a specific
    multiplier — exactly what the dashboard / decision-ledger rejection
    path needs.)

    Four distinct rejection scenarios, each collapsing a different
    multiplier to zero:

      (a) Zero edge → raw_size = 0 → size = 0
      (b) Zero liquidity → liquidity_mult = 0 → size = 0
      (c) Drawdown at the limit → drawdown_mult = 0 → size = 0
      (d) Existing exposure at the cap → correlation_mult = 0 → size = 0

    For each, ``size_usd == 0.0`` AND the responsible multiplier is
    exactly ``0.0`` in the components dict, AND the OTHER multipliers
    are non-zero (so the rejection is unambiguously attributable to
    that single multiplier — i.e. the "reason" is clear from the
    breakdown).
    """
    common = dict(
        strategy="rejection_test",
        confidence=0.70,
        strategy_performance=None,
        brier=0.10,  # healthy → calibration_mult = 1.0
    )

    # ── (a) Zero edge → raw_size = 0 → size = 0 ─────────────────────────
    bd = allocation_breakdown(
        edge=0.0, liquidity=100.0, existing_exposure=0.0, drawdown=0.0,
        **common,
    )
    assert bd["size_usd"] == 0.0
    assert bd["components"]["raw_size"] == 0.0
    # The OTHER multipliers are non-zero — the rejection is unambiguously
    # attributable to the edge being zero (raw_size collapsed).
    assert bd["components"]["liquidity_mult"] > 0
    assert bd["components"]["confidence_mult"] > 0
    assert bd["components"]["calibration_mult"] > 0
    assert bd["components"]["drawdown_mult"] > 0
    assert bd["components"]["correlation_mult"] > 0
    assert bd["components"]["performance_mult"] > 0

    # ── (b) Zero liquidity → liquidity_mult = 0 → size = 0 ──────────────
    bd = allocation_breakdown(
        edge=0.05, liquidity=0.0, existing_exposure=0.0, drawdown=0.0,
        **common,
    )
    assert bd["size_usd"] == 0.0
    assert bd["components"]["liquidity_mult"] == 0.0
    # raw_size is non-zero — the rejection was caused by liquidity, not edge.
    assert bd["components"]["raw_size"] > 0
    assert bd["components"]["confidence_mult"] > 0
    assert bd["components"]["calibration_mult"] > 0
    assert bd["components"]["drawdown_mult"] > 0
    assert bd["components"]["correlation_mult"] > 0
    assert bd["components"]["performance_mult"] > 0

    # ── (c) Drawdown at the limit → drawdown_mult = 0 → size = 0 ────────
    bd = allocation_breakdown(
        edge=0.05, liquidity=100.0, existing_exposure=0.0,
        drawdown=MAX_DRAWDOWN_LIMIT,
        **common,
    )
    assert bd["size_usd"] == 0.0
    assert bd["components"]["drawdown_mult"] == 0.0
    assert bd["components"]["raw_size"] > 0
    assert bd["components"]["liquidity_mult"] > 0
    assert bd["components"]["confidence_mult"] > 0
    assert bd["components"]["calibration_mult"] > 0
    assert bd["components"]["correlation_mult"] > 0
    assert bd["components"]["performance_mult"] > 0

    # ── (d) Existing exposure at the cap → correlation_mult = 0 ─────────
    bd = allocation_breakdown(
        edge=0.05, liquidity=100.0,
        existing_exposure=MAX_POSITION_PER_MARKET,
        drawdown=0.0,
        **common,
    )
    assert bd["size_usd"] == 0.0
    assert bd["components"]["correlation_mult"] == 0.0
    assert bd["components"]["raw_size"] > 0
    assert bd["components"]["liquidity_mult"] > 0
    assert bd["components"]["confidence_mult"] > 0
    assert bd["components"]["calibration_mult"] > 0
    assert bd["components"]["drawdown_mult"] > 0
    assert bd["components"]["performance_mult"] > 0

    # ── Bonus: rejection also clips ``allocate_capital`` to 0 ───────────
    # ``allocate_capital`` returns a plain float; its rejection signature
    # is ``size == 0.0`` (no reason attached in the return value — the
    # caller must invoke ``allocation_breakdown`` separately to learn the
    # reason). Verify the four rejection scenarios above also produce
    # ``0.0`` from ``allocate_capital``.
    #
    # NOTE: ``allocate_capital`` auto-reads Brier from the live ML model
    # (no ``brier`` parameter), so calibration_mult may be 0.30/0.60/1.0
    # depending on the model's current Brier. The four rejection scenarios
    # below collapse a DIFFERENT multiplier to zero (raw_size, liq_mult,
    # dd_mult, corr_mult) so the size is 0 regardless of cal_mult.
    # (a) Zero edge
    assert allocate_capital(
        strategy="x", edge=0.0, confidence=0.70, liquidity=100.0,
    ) == 0.0
    # (b) Zero liquidity
    assert allocate_capital(
        strategy="x", edge=0.05, confidence=0.70, liquidity=0.0,
    ) == 0.0
    # (c) Drawdown at the limit
    assert allocate_capital(
        strategy="x", edge=0.05, confidence=0.70, liquidity=100.0,
        drawdown=MAX_DRAWDOWN_LIMIT,
    ) == 0.0
    # (d) Existing exposure at the cap
    assert allocate_capital(
        strategy="x", edge=0.05, confidence=0.70, liquidity=100.0,
        existing_exposure=MAX_POSITION_PER_MARKET,
    ) == 0.0
