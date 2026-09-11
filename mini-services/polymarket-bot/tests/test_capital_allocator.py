"""
tests/test_capital_allocator.py — Unit tests for ``core/capital_allocator.py``.

T9 — Capital Allocator unit tests.

Covers the eight behaviours required by the task spec:

  (1) ``allocate_size`` returns ``0.0`` when ``edge == 0``.
  (2) ``allocate_size`` returns ``0.0`` when ``confidence < 0.45``.
  (3) 4× edge yields strictly less than 2× size (sublinear / saturating
      curve).
  (4) ``allocate_size`` returns ``0.0`` when ``drawdown > $8``.
  (5) ``allocate_size`` returns ``0.0`` when ``existing_exposure > $5``.
  (6) ``allocate_size`` returns ``0.0`` when ``liquidity == 0``.
  (7) The suggested size is capped at ``$3.00``.
  (8) The suggested size is floored at ``$0.50`` (for non-zero returns).

The allocator is a **pure, stateless, synchronous** function — no DB,
no singleton, no async. Every test is a plain ``def`` (no ``async def``)
and runs without an event loop. The repo's ``pytest.ini`` declares
``testpaths = tests``; this file is collected automatically.

Conventions
------------
* ``sys.path`` is bootstrapped so the test runs regardless of the cwd
  pytest was launched from (mirrors the bootstrap pattern in
  ``tests/test_decision_ledger.py``, ``tests/test_risk_manager.py``).
* The env-var redirect block at module top is **defensive only** — the
  capital allocator itself reads no env vars and imports no other
  project module, so it would run cleanly without any redirect. But the
  sibling test files in the same pytest session *do* read env vars at
  import time (``core.data_store``, ``risk.manager``, …), and the
  ``pytest.ini::testpaths = tests`` discovery pattern means pytest
  imports the whole ``tests/`` package before running any single file.
  Setting the redirects here (with ``setdefault`` so an outer runner
  can override) keeps the file's *neighbours* hermetic even if a future
  test run happens to import this file alongside a stateful one.
* The constants ``MIN_CONFIDENCE``, ``MAX_DRAWDOWN_USD``,
  ``MAX_EXISTING_EXPOSURE_USD``, ``MAX_SIZE_USD``, ``MIN_SIZE_USD`` are
  imported from the module under test so the assertions stay in lock-
  step with the implementation (a future re-tune of the threshold moves
  the test automatically, rather than silently breaking it).
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
_TMP_ROOT = Path("/tmp/capital_allocator_tests")
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
# (``core.*``) regardless of the cwd pytest was launched from. Mirrors
# the bootstrap pattern in tests/test_features.py / test_paper_simulator.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.capital_allocator import (  # noqa: E402
    MAX_DRAWDOWN_USD,
    MAX_EXISTING_EXPOSURE_USD,
    MAX_SIZE_USD,
    MIN_CONFIDENCE,
    MIN_SIZE_USD,
    SIZE_CURVE_EXPONENT,
    SIZE_SCALE,
    allocate_size,
)

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here — every
# test in this file is a plain synchronous ``def``. The allocator is a pure
# function (no I/O, no awaits) so there is nothing for the asyncio event
# loop to schedule. Skipping the asyncio marker keeps pytest-asyncio
# collection cost off this file entirely.


# ── Helpers ────────────────────────────────────────────────────────────────
def _baseline_kwargs() -> dict:
    """Return kwargs that clear EVERY safety gate and produce a raw size
    comfortably inside the [$0.50, $3.00] band — i.e. neither clipped to
    the floor nor clipped to the cap.

    Each test below overrides exactly ONE of these values to trip the gate
    under test (or to push the raw size past a bound), so the assertion
    can attribute the result to that single variable rather than to a
    confounding gate.

    Chosen values (with the implementation's ``SIZE_SCALE * edge ** 0.4 *
    confidence`` formula):

        raw = 5.0 * 0.05 ** 0.4 * 0.70
            = 5.0 * 0.2973 * 0.70
            ≈ $1.04

    So the baseline produces a $1.04 suggested size — strictly between
    the $0.50 floor and the $3.00 cap, with ~$0.54 of headroom below
    the cap and ~$0.54 above the floor. That headroom is what makes the
    saturation test (test 3) meaningful: doubling edge from 0.05 → 0.20
    lands at raw ≈ $1.84, still well inside the band, so the < 2× ratio
    is provably a property of the *curve*, not of the cap clipping the
    upper sample.
    """
    return dict(
        edge=0.05,           # 5 % predicted edge — typical Polymarket BUY
        confidence=0.70,     # well above the 0.45 floor
        drawdown=0.0,        # no drawdown from peak
        existing_exposure=0.0,  # nothing open on this market yet
        liquidity=1_000.0,   # $1k of book depth — comfortably non-zero
    )


# ── (1) edge == 0 → 0.0 ───────────────────────────────────────────────────
def test_returns_zero_for_zero_edge():
    """A zero edge means "no predicted advantage" — the allocator must
    refuse to size the trade (return exactly ``0.0``), regardless of how
    confident the model is or how liquid the book is.

    This is the first safety gate in ``allocate_size`` and is evaluated
    *before* the floor/cap logic, so even though the floor would
    otherwise raise a sub-floor raw size to ``$0.50``, a zero-edge trade
    returns the literal ``0.0`` — not ``$0.50``. The two values are
    semantically distinct: ``0.0`` means "do not trade"; ``$0.50`` means
    "trade the minimum". A regression that floored zero-edge trades to
    $0.50 would silently open minimum-size orders on noise.
    """
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 0.0

    size = allocate_size(**kwargs)

    assert size == 0.0
    # Belt-and-braces: pin the type too. A regression that returned
    # ``0`` (int) instead of ``0.0`` (float) would still pass ``== 0.0``
    # but would break downstream code that calls ``float.__round__`` on
    # the result.
    assert isinstance(size, float)
    # And pin the absence of the floor — this is the test's reason for
    # existing separately from the cap test: ``0.0`` is NOT floored to
    # ``MIN_SIZE_USD`` ($0.50).
    assert size != MIN_SIZE_USD


# ── (2) confidence < 0.45 → 0.0 ────────────────────────────────────────────
def test_returns_zero_for_confidence_below_threshold():
    """Below ``MIN_CONFIDENCE`` (0.45) the allocator must return ``0.0``
    regardless of how large the edge is.

    The threshold is strict (``<``, not ``<=``): a confidence of exactly
    0.45 should pass this gate (and the test below pins that boundary
    too). We exercise the strict-inequality boundary by sampling just
    below (0.44999…) and just at (0.45 exactly) the threshold.
    """
    # Just below the threshold — must return 0.0.
    kwargs = _baseline_kwargs()
    kwargs["confidence"] = MIN_CONFIDENCE - 0.0001  # 0.4499
    assert allocate_size(**kwargs) == 0.0

    # Exactly at the threshold — must NOT return 0.0 (strict ``<`` gate).
    # The raw size is $1.04 (well inside the band), so a non-zero return
    # here is unambiguously "the gate did not trip" rather than "the
    # gate tripped and the floor raised zero to $0.50".
    kwargs = _baseline_kwargs()
    kwargs["confidence"] = MIN_CONFIDENCE  # 0.45 exactly
    size_at_threshold = allocate_size(**kwargs)
    assert size_at_threshold != 0.0
    assert MIN_SIZE_USD <= size_at_threshold <= MAX_SIZE_USD

    # Even an enormous edge cannot rescue a sub-threshold confidence.
    # Belt-and-braces: the gate must fire regardless of the other inputs.
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 1.0  # 100 % edge — the largest possible
    kwargs["confidence"] = 0.10  # deeply sub-threshold confidence
    assert allocate_size(**kwargs) == 0.0


# ── (3) 4× edge → < 2× size (saturating) ─────────────────────────────────
def test_four_x_edge_yields_less_than_two_x_size():
    """The size curve must be **strictly sublinear** in ``edge`` so that
    a 4× larger edge produces strictly less than 2× the suggested size.

    The institutional intuition: doubling down on a 2× stronger thesis
    should NOT double the bet — the model's edge estimate is noisy, so
    the allocator should taper its size growth as edge increases (Kelly
    on a noisy estimate, basically). A linear curve would size 4× the
    bet on 4× the edge; a square-root curve would size exactly 2× the
    bet; the contract here is *strictly less than* 2× the bet.

    Implementation guarantee
    ------------------------
    The raw size formula is ``SIZE_SCALE * edge ** SIZE_CURVE_EXPONENT *
    confidence``. The saturation ratio for a 4× edge multiplier is:

        raw(4e) / raw(e) = (4e) ** α / e ** α = 4 ** α

    where ``α = SIZE_CURVE_EXPONENT``. For ``α < 0.5`` this ratio is
    strictly less than ``4 ** 0.5 = 2``. The implementation uses
    ``α = 0.4``, giving a ratio of ``4 ** 0.4 ≈ 1.741`` — comfortably
    below 2 (a ~13 % margin to absorb float round-off without flipping
    the strict ``<`` test).

    Test methodology
    ----------------
    We pick ``edge_low = 0.05`` and ``edge_high = 4 * edge_low = 0.20``.
    Both produce raw sizes well inside the [$0.50, $3.00] band
    (≈ $1.04 and ≈ $1.84 respectively), so the saturation ratio is a
    property of the *curve*, not of the cap clipping the upper sample.
    If the implementation regressed to a linear curve (exponent = 1.0),
    the ratio would be exactly 4.0 and this test would fail loudly.
    """
    kwargs_low = _baseline_kwargs()
    kwargs_low["edge"] = 0.05
    size_low = allocate_size(**kwargs_low)

    kwargs_high = _baseline_kwargs()
    kwargs_high["edge"] = 4 * kwargs_low["edge"]  # 0.20
    size_high = allocate_size(**kwargs_high)

    # Sanity: both samples are non-zero and inside the band (i.e. neither
    # the floor nor the cap is clipping the result — the ratio we're
    # about to assert is a property of the curve, not of the bounds).
    assert size_low > 0.0, "baseline size_low must be non-zero"
    assert size_high > 0.0, "baseline size_high must be non-zero"
    assert MIN_SIZE_USD < size_low < MAX_SIZE_USD, (
        f"size_low ({size_low:.4f}) must be strictly inside "
        f"[$MIN_SIZE_USD, $MAX_SIZE_USD] so the saturation ratio is a "
        f"property of the curve, not of the floor clipping size_low up."
    )
    assert MIN_SIZE_USD < size_high < MAX_SIZE_USD, (
        f"size_high ({size_high:.4f}) must be strictly inside "
        f"[$MIN_SIZE_USD, $MAX_SIZE_USD] so the saturation ratio is a "
        f"property of the curve, not of the cap clipping size_high down."
    )

    # Monotonicity sanity: 4× edge must NOT shrink the size. (A buggy
    # curve like ``1 / edge`` would pass the < 2× test below but fail
    # this monotonicity check.)
    assert size_high > size_low, (
        "size_high must be strictly greater than size_low — a saturating "
        "curve is monotonic non-decreasing; if 4× edge shrinks the size "
        "the curve is wrong (not just sublinear)."
    )

    # The T9 task contract: 4× edge yields STRICTLY LESS than 2× size.
    assert size_high < 2.0 * size_low, (
        f"4× edge must yield < 2× size (saturating). Got size_low="
        f"{size_low:.4f}, size_high={size_high:.4f}, ratio="
        f"{size_high / size_low:.4f} (must be < 2.0)."
    )

    # Belt-and-braces: pin the analytical saturation ratio so a future
    # re-tune of ``SIZE_CURVE_EXPONENT`` (e.g. to 0.5 — which would make
    # the ratio exactly 2.0 and silently break the strict ``<`` test)
    # trips this assertion too.
    expected_ratio = 4.0 ** SIZE_CURVE_EXPONENT
    actual_ratio = size_high / size_low
    assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6), (
        f"saturation ratio must equal 4 ** SIZE_CURVE_EXPONENT "
        f"({expected_ratio:.6f}); got {actual_ratio:.6f}. If "
        f"SIZE_CURVE_EXPONENT was tuned to >= 0.5 the ratio would "
        f"be >= 2.0 and the saturating contract would break."
    )
    # And the exponent itself must remain strictly below 0.5 — otherwise
    # the analytic guarantee above stops holding.
    assert SIZE_CURVE_EXPONENT < 0.5, (
        "SIZE_CURVE_EXPONENT must be strictly less than 0.5 to guarantee "
        "4× edge → < 2× size for every valid edge value. A value of "
        "exactly 0.5 would make the ratio exactly 2.0 (failing the strict "
        "< test on float round-off); a value above 0.5 would make the "
        "ratio exceed 2.0 (failing the contract outright)."
    )


# ── (4) drawdown > $8 → 0.0 ───────────────────────────────────────────────
def test_returns_zero_when_drawdown_exceeds_limit():
    """When the peak-to-trough drawdown exceeds ``MAX_DRAWDOWN_USD`` ($8)
    the allocator must return ``0.0`` — the institutional MDD breaker
    would halt trading at the same threshold, but sizing to zero here
    avoids suggesting an order that the risk gate would reject.

    The gate is strict (``>``, not ``>=``): a drawdown of exactly $8.00
    must NOT trip this gate (the boundary belongs to the risk engine's
    ``MAX_DRAWDOWN_LIMIT`` check, where ``>=`` is the right semantic).
    """
    # Above the threshold — must return 0.0.
    kwargs = _baseline_kwargs()
    kwargs["drawdown"] = MAX_DRAWDOWN_USD + 0.01  # $8.01
    assert allocate_size(**kwargs) == 0.0

    # Even a tiny overshoot trips the gate.
    kwargs = _baseline_kwargs()
    kwargs["drawdown"] = MAX_DRAWDOWN_USD + 1e-9
    assert allocate_size(**kwargs) == 0.0

    # Exactly at the threshold — must NOT return 0.0 (strict ``>`` gate).
    # The baseline raw size ($1.04) is well inside the band, so a
    # non-zero return here is unambiguously "the gate did not trip".
    kwargs = _baseline_kwargs()
    kwargs["drawdown"] = MAX_DRAWDOWN_USD  # $8.00 exactly
    size_at_threshold = allocate_size(**kwargs)
    assert size_at_threshold != 0.0
    assert MIN_SIZE_USD <= size_at_threshold <= MAX_SIZE_USD

    # Belt-and-braces: a deeply-over-the-line drawdown cannot be rescued
    # by an otherwise-perfect setup.
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 1.0
    kwargs["confidence"] = 1.0
    kwargs["drawdown"] = 100.0  # $100 drawdown on a $100 operating bankroll
    assert allocate_size(**kwargs) == 0.0


# ── (5) existing_exposure > $5 → 0.0 ──────────────────────────────────────
def test_returns_zero_when_existing_exposure_exceeds_limit():
    """When existing open exposure on the same market / correlated group
    exceeds ``MAX_EXISTING_EXPOSURE_USD`` ($5) the allocator must return
    ``0.0`` — concentration discipline says we don't keep doubling down
    on a single thesis past this point.

    Same strict-inequality semantics as the drawdown gate (test 4):
    exactly $5.00 must NOT trip; $5.01 must trip.
    """
    # Above the threshold — must return 0.0.
    kwargs = _baseline_kwargs()
    kwargs["existing_exposure"] = MAX_EXISTING_EXPOSURE_USD + 0.01  # $5.01
    assert allocate_size(**kwargs) == 0.0

    # Even a tiny overshoot trips the gate.
    kwargs = _baseline_kwargs()
    kwargs["existing_exposure"] = MAX_EXISTING_EXPOSURE_USD + 1e-9
    assert allocate_size(**kwargs) == 0.0

    # Exactly at the threshold — must NOT return 0.0 (strict ``>`` gate).
    kwargs = _baseline_kwargs()
    kwargs["existing_exposure"] = MAX_EXISTING_EXPOSURE_USD  # $5.00 exactly
    size_at_threshold = allocate_size(**kwargs)
    assert size_at_threshold != 0.0
    assert MIN_SIZE_USD <= size_at_threshold <= MAX_SIZE_USD

    # Belt-and-braces: an enormous edge cannot rescue an over-concentrated
    # book — even a 100 % edge with a fully-confident model on a deeply
    # liquid market must not be sized when we're already $100 into the
    # same group.
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 1.0
    kwargs["confidence"] = 1.0
    kwargs["liquidity"] = 1_000_000.0
    kwargs["existing_exposure"] = 100.0
    assert allocate_size(**kwargs) == 0.0


# ── (6) liquidity == 0 → 0.0 ───────────────────────────────────────────────
def test_returns_zero_when_liquidity_is_zero():
    """When book liquidity is zero the allocator must return ``0.0`` —
    submitting into an empty book would cross the spread by the full
    remaining ask (or, worse, fail to fill at all).

    The gate uses ``liquidity <= 0`` (not strict ``<``) so it also
    catches negative sentinels a buggy upstream might emit. We exercise
    both ``liquidity == 0`` (the T9 contract) and ``liquidity < 0``
    (the defensive extension).
    """
    # Exactly zero — must return 0.0 (the T9 contract).
    kwargs = _baseline_kwargs()
    kwargs["liquidity"] = 0.0
    assert allocate_size(**kwargs) == 0.0

    # Negative — must also return 0.0 (defensive ``<= 0`` gate).
    kwargs = _baseline_kwargs()
    kwargs["liquidity"] = -1.0
    assert allocate_size(**kwargs) == 0.0

    # Belt-and-braces: a tiny positive liquidity must NOT trip this gate
    # (the contract is ``liquidity == 0`` → 0.0, not "thin liquidity"
    # → 0.0). A $0.01-depth book is still technically non-empty and the
    # allocator's job is sizing, not liquidity-quality judging — the
    # risk engine handles the latter via its $200-minimum-depth gate.
    kwargs = _baseline_kwargs()
    kwargs["liquidity"] = 0.01  # one cent of depth — vanishingly thin
    size_thin = allocate_size(**kwargs)
    assert size_thin != 0.0, (
        "liquidity > 0 (even $0.01) must NOT trip the liquidity gate; "
        "the contract is `liquidity == 0 → 0.0`, not `liquidity < threshold "
        "→ 0.0`. Thin-liquidity rejection is the risk engine's job."
    )
    assert MIN_SIZE_USD <= size_thin <= MAX_SIZE_USD


# ── (7) size capped at $3.00 ──────────────────────────────────────────────
def test_size_capped_at_max():
    """The suggested size must never exceed ``MAX_SIZE_USD`` ($3.00),
    regardless of how large the edge or confidence is.

    We force the raw size past the cap by setting ``edge = 1.0`` and
    ``confidence = 1.0``:

        raw = SIZE_SCALE * 1.0 ** 0.4 * 1.0
            = SIZE_SCALE * 1.0 * 1.0
            = $5.00

    $5.00 > $3.00 → the cap must clip the return to exactly $3.00.
    """
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 1.0       # 100 % edge — the maximum possible
    kwargs["confidence"] = 1.0  # 100 % confidence — the maximum possible

    size = allocate_size(**kwargs)

    assert size == MAX_SIZE_USD
    # Belt-and-braces: pin the literal value too so a future re-tune of
    # MAX_SIZE_USD that forgot to update this test would fail loudly
    # (rather than silently asserting ``size == new_value``).
    assert size == 3.0
    assert isinstance(size, float)


# ── (8) size floored at $0.50 ─────────────────────────────────────────────
def test_size_floored_at_min():
    """When the raw size would be below ``MIN_SIZE_USD`` ($0.50) but
    every safety gate passes, the allocator must return ``$0.50`` (not
    the sub-floor raw value, and not ``0.0``).

    We force the raw size below the floor by setting ``edge = 0.0001``
    (a 1-basis-point edge — barely above zero) and ``confidence = 0.50``
    (just above the 0.45 threshold so the confidence gate does NOT
    trip — the floor is what we're testing, not the confidence gate):

        raw = SIZE_SCALE * 0.0001 ** 0.4 * 0.50
            = 5.0 * 0.03981 * 0.50
            ≈ $0.0995

    $0.0995 < $0.50 → the floor must clip the return UP to exactly
    $0.50.

    Crucially, ``$0.50`` is NOT the same as ``$0.0``:
      * ``$0.0``  = "do not trade" (a safety gate tripped)
      * ``$0.50`` = "trade the minimum" (gates passed, raw size floored)
    A regression that conflated the two (e.g. flooring ``raw < MIN_SIZE``
    to ``0.0`` instead of to ``MIN_SIZE_USD``) would silently suppress
    minimum-size trades on small edges — this test pins them apart.
    """
    kwargs = _baseline_kwargs()
    kwargs["edge"] = 0.0001     # 1 bp edge — barely above zero
    kwargs["confidence"] = 0.50  # just above the 0.45 floor (gate does NOT trip)

    size = allocate_size(**kwargs)

    assert size == MIN_SIZE_USD
    # Belt-and-braces: pin the literal value too.
    assert size == 0.50
    assert isinstance(size, float)

    # The floor must NOT be confused with the safety-gate-zero return.
    # Sanity: an edge of EXACTLY zero returns 0.0 (test 1), but an edge
    # of 0.0001 returns $0.50 — the two paths are distinct and the test
    # pins that distinction by asserting the floored return is non-zero.
    assert size != 0.0, (
        "Floored size must be $0.50, NOT $0.0. A regression that returned "
        "0.0 for sub-floor raw sizes would conflate 'do not trade' (gate "
        "tripped) with 'trade the minimum' (gate passed, size floored)."
    )


# ── Bonus: baseline sanity check ──────────────────────────────────────────
def test_baseline_kwargs_produce_in_band_non_zero_size():
    """Sanity check on the ``_baseline_kwargs`` helper itself: the
    baseline must produce a non-zero size strictly inside the
    ``[$0.50, $3.00]`` band (i.e. neither floored nor capped).

    This isn't one of the eight T9 contracts — it's a regression guard
    on the test fixture itself. If a future edit to ``SIZE_SCALE`` or
    ``SIZE_CURVE_EXPONENT`` moved the baseline raw size outside the
    band, tests 3 and 4-5 (which rely on the baseline being in-band to
    isolate the variable under test) would silently become no-ops.
    This test fails loudly in that case.
    """
    size = allocate_size(**_baseline_kwargs())

    assert size > 0.0
    assert MIN_SIZE_USD < size < MAX_SIZE_USD, (
        f"baseline raw size ({size:.4f}) must be strictly inside "
        f"[$MIN_SIZE_USD, $MAX_SIZE_USD] so the saturation / gate tests "
        f"that override a single baseline variable can attribute their "
        f"result to that variable. If SIZE_SCALE or SIZE_CURVE_EXPONENT "
        f"moved the baseline outside the band, update _baseline_kwargs "
        f"to compensate."
    )

    # Pin the analytic baseline value too: 5.0 * 0.05 ** 0.4 * 0.70.
    expected = SIZE_SCALE * (0.05 ** SIZE_CURVE_EXPONENT) * 0.70
    assert size == pytest.approx(expected, rel=1e-9)
