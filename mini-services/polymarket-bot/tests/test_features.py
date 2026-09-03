"""
tests/test_features.py — Unit tests for ml/features.py.

Covers the five behaviours required by S6:
  (1) `extract_features` returns a 38-dim float32 numpy array for a valid book.
  (2) `extract_features` returns None when `mid` is None / ≤ 0.001 / ≥ 0.999
      (the three documented rejection paths at the top of the function).
  (3) Order-Flow-Imbalance (OFI, feature index 2) matches the formula
      `(best_bid_sz - best_ask_sz) / max(best_bid_sz + best_ask_sz, 1.0)`
      for several known bid/ask size pairs.
  (4) Competitiveness (feature index 22) is derived from the live spread
      (R14 train/serve-skew fix), NOT from `market.get("competitive")`.
  (5) The returned feature vector never contains NaN or Inf across a matrix
      of realistic + adversarial inputs (zero sizes, huge sizes, empty
      market dict, extreme mids, very tight spread, deep 5-level book, …).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional

# Inline sys.path bootstrap — mirrors the pattern in test_paper_simulator.py
# and the (docstring-only) tests/conftest.py promise. Required so the test
# module can `from core.data_store import ...` regardless of the cwd pytest
# was launched from (monorepo root, CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from core.data_store import OrderBook, PriceLevel  # noqa: E402
from ml import features  # noqa: E402
from ml.features import FEATURE_NAMES, N_FEATURES, extract_features  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_book(
    bid_price: Optional[float] = 0.49,
    bid_size: float = 100.0,
    ask_price: Optional[float] = 0.51,
    ask_size: float = 100.0,
    token_id: str = "TEST_TOKEN_S6",
) -> OrderBook:
    """Build a single-level OrderBook. Pass None for bid_price/ask_price to
    omit that side entirely (forces `mid` to be None)."""
    bids = [PriceLevel(price=bid_price, size=bid_size)] if bid_price is not None else []
    asks = [PriceLevel(price=ask_price, size=ask_size)] if ask_size is not None else []
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


def _basic_market() -> dict:
    return {
        "volume24hr": 1_000.0,
        "volume": 7_000.0,
        "liquidity": 5_000.0,
    }


@pytest.fixture(autouse=True)
def _reset_price_history():
    """Clear the module-level `_price_history` deque cache between tests so
    state from one test cannot leak into another (the Hurst / momentum /
    rolling-volatility features consume that history)."""
    features._price_history.clear()
    yield
    features._price_history.clear()


# ── (1) Shape & dtype ─────────────────────────────────────────────────────────

def test_extract_features_returns_38_dim_float32_array_for_valid_book():
    book = _make_book(bid_price=0.49, bid_size=100.0, ask_price=0.51, ask_size=100.0)
    vec = extract_features(_basic_market(), book)

    assert vec is not None, "Expected a feature vector for a valid book, got None"
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (N_FEATURES,), f"Expected ({N_FEATURES},), got {vec.shape}"
    assert vec.dtype == np.float32, f"Expected float32, got {vec.dtype}"


def test_n_features_constant_and_feature_names_length_are_38():
    assert N_FEATURES == 38
    assert len(FEATURE_NAMES) == 38


# ── (2) None rejection paths ─────────────────────────────────────────────────

def test_extract_features_returns_none_for_mid_none_no_bids():
    # No bids → OrderBook.mid is None
    book = _make_book(bid_price=None, ask_price=0.51)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_returns_none_for_mid_none_no_asks():
    # No asks → OrderBook.mid is None
    book = _make_book(bid_price=0.49, ask_price=None)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_returns_none_for_mid_at_floor():
    # mid = 0.001 → matches `mid <= 0.001` → rejected
    book = _make_book(bid_price=0.001, ask_price=0.001)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_returns_none_for_mid_below_floor():
    # mid = 0.0005 → matches `mid <= 0.001` → rejected
    book = _make_book(bid_price=0.0002, ask_price=0.0008)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_accepts_mid_just_above_floor():
    # mid = 0.0015 → NOT rejected by `mid <= 0.001` → returns a vector
    book = _make_book(bid_price=0.001, ask_price=0.002)
    vec = extract_features(_basic_market(), book)
    assert vec is not None
    assert vec.shape == (N_FEATURES,)


def test_extract_features_returns_none_for_mid_at_ceiling():
    # mid = 0.999 → matches `mid >= 0.999` → rejected
    book = _make_book(bid_price=0.999, ask_price=0.999)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_returns_none_for_mid_above_ceiling():
    # mid = 0.9995 → matches `mid >= 0.999` → rejected
    book = _make_book(bid_price=0.999, ask_price=1.000)
    assert extract_features(_basic_market(), book) is None


def test_extract_features_accepts_mid_just_below_ceiling():
    # mid = 0.9985 → NOT rejected by `mid >= 0.999` → returns a vector
    book = _make_book(bid_price=0.998, ask_price=0.999)
    vec = extract_features(_basic_market(), book)
    assert vec is not None
    assert vec.shape == (N_FEATURES,)


# ── (3) OFI calculation correctness ───────────────────────────────────────────

OFI_INDEX = FEATURE_NAMES.index("order_flow_imbalance")
assert OFI_INDEX == 2, "OFI must be feature index 2 (per ml/features.py docstring)"


@pytest.mark.parametrize(
    "bid_sz, ask_sz, expected_ofi",
    [
        # Symmetric book → OFI = 0
        (100.0, 100.0, 0.0),
        # Bid-heavy: (200 - 100) / 300 = 0.3333
        (200.0, 100.0, (200.0 - 100.0) / 300.0),
        # Ask-heavy: (100 - 200) / 300 = -0.3333
        (100.0, 200.0, (100.0 - 200.0) / 300.0),
        # All bid, no ask size: top_depth=100, OFI = 100/100 = 1.0
        (100.0, 0.0, 1.0),
        # All ask, no bid size: OFI = -100/100 = -1.0
        (0.0, 100.0, -1.0),
        # Both zero: top_depth=max(0,1.0)=1.0, OFI = 0/1 = 0.0
        (0.0, 0.0, 0.0),
    ],
)
def test_ofi_matches_formula_for_known_bid_ask_sizes(bid_sz, ask_sz, expected_ofi):
    book = _make_book(
        bid_price=0.49, bid_size=bid_sz,
        ask_price=0.51, ask_size=ask_sz,
    )
    vec = extract_features(_basic_market(), book)
    assert vec is not None, "extract_features unexpectedly returned None"
    ofi = float(vec[OFI_INDEX])
    assert math.isclose(ofi, expected_ofi, rel_tol=1e-5, abs_tol=1e-6), (
        f"OFI for bid={bid_sz}, ask={ask_sz}: expected {expected_ofi:.6f}, got {ofi:.6f}"
    )


# ── (4) Competitiveness derived from spread, not market.get("competitive") ───

COMP_INDEX = FEATURE_NAMES.index("competitiveness")
assert COMP_INDEX == 22, "competitiveness must be feature index 23 (1-based) / 22 (0-based)"


def _expected_competitiveness(spread: float) -> float:
    """Mirror of the R14 derivation in ml/features.py:
        spread_used = book.spread or 0.01     # 0.0 (locked book) is falsy → 0.01
        spread_for_comp = max(spread_used, 0.001)
        competitiveness = clip(1.0 - spread_for_comp / 0.05, -1.0, 1.0)
    """
    # `extract_features` uses `book.spread or 0.01`; when the real spread is
    # exactly 0.0 (locked book), 0.0 is falsy so the effective spread becomes 0.01.
    spread_used = spread if spread > 0 else 0.01
    spread_for_comp = max(spread_used, 0.001)
    return float(np.clip(1.0 - (spread_for_comp / 0.05), -1.0, 1.0))


@pytest.mark.parametrize(
    "bid_p, ask_p, spurious_competitive_value",
    [
        # Tight spread 0.02 → compet = 0.6; market["competitive"] is a string
        # that must be ignored entirely.
        (0.49, 0.51, "garbage_string_that_must_be_ignored"),
        # Same spread, market["competitive"] is int 1 (max-competitive sentinel).
        (0.49, 0.51, 1),
        # Wide spread 0.10 → compet = -1.0; market["competitive"] is float 0.99
        # (would otherwise look like a hyper-competitive market).
        (0.45, 0.55, 0.99),
        # Zero spread (crossed/locked book) → compet = clip(1 - 0.001/0.05) = 0.98
        (0.50, 0.50, "ignored"),
        # Negative test: market["competitive"] = None should also be ignored
        (0.49, 0.51, None),
    ],
)
def test_competitiveness_derived_from_spread_not_from_market_dict(
    bid_p, ask_p, spurious_competitive_value
):
    market = _basic_market()
    # Inject a spurious "competitive" value that must NOT influence the feature.
    market["competitive"] = spurious_competitive_value

    book = _make_book(bid_price=bid_p, ask_price=ask_p, bid_size=100.0, ask_size=100.0)
    vec = extract_features(market, book)
    assert vec is not None, "extract_features unexpectedly returned None"

    spread = ask_p - bid_p
    expected = _expected_competitiveness(spread)
    actual = float(vec[COMP_INDEX])
    assert math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-6), (
        f"spread={spread:.4f}: expected competitiveness {expected:.6f}, got {actual:.6f} "
        f"(market['competitive']={spurious_competitive_value!r} must be ignored)"
    )


def test_competitiveness_varies_with_spread_when_market_dict_is_identical():
    """Holding the market dict identical, varying book spread must vary the
    competitiveness feature — proves the value comes from `book.spread`,
    not from a constant or a market field."""
    market = _basic_market()

    # Tight spread 0.01 → high competitiveness
    tight_book = _make_book(bid_price=0.495, ask_price=0.505, bid_size=100.0, ask_size=100.0)
    tight_vec = extract_features(market, tight_book)

    # Wide spread 0.10 → low competitiveness
    wide_book = _make_book(bid_price=0.45, ask_price=0.55, bid_size=100.0, ask_size=100.0)
    wide_vec = extract_features(market, wide_book)

    assert tight_vec is not None and wide_vec is not None
    tight_comp = float(tight_vec[COMP_INDEX])
    wide_comp = float(wide_vec[COMP_INDEX])
    assert tight_comp > wide_comp, (
        f"Expected tight spread → higher competitiveness; "
        f"got tight={tight_comp}, wide={wide_comp}"
    )
    # And both must match their spread-derived expectations exactly.
    assert math.isclose(tight_comp, _expected_competitiveness(0.01), rel_tol=1e-5, abs_tol=1e-6)
    assert math.isclose(wide_comp, _expected_competitiveness(0.10), rel_tol=1e-5, abs_tol=1e-6)


# ── (5) No NaN / Inf in feature vector ──────────────────────────────────────

@pytest.mark.parametrize(
    "label, book, market",
    [
        (
            "typical_book",
            _make_book(0.49, 100.0, 0.51, 100.0),
            _basic_market(),
        ),
        (
            "empty_sizes",
            _make_book(0.49, 0.0, 0.51, 0.0),
            _basic_market(),
        ),
        (
            "huge_sizes",
            _make_book(0.49, 1_000_000.0, 0.51, 1_000_000.0),
            _basic_market(),
        ),
        (
            "tiny_sizes",
            _make_book(0.49, 1e-9, 0.51, 1e-9),
            _basic_market(),
        ),
        (
            "missing_market_fields",
            _make_book(0.49, 100.0, 0.51, 100.0),
            {},
        ),
        (
            "zero_volume_and_liquidity",
            _make_book(0.49, 100.0, 0.51, 100.0),
            {"volume24hr": 0.0, "volume": 0.0, "liquidity": 0.0},
        ),
        (
            "extreme_mid_high",
            _make_book(0.97, 100.0, 0.98, 100.0),
            _basic_market(),
        ),
        (
            "extreme_mid_low",
            _make_book(0.02, 100.0, 0.03, 100.0),
            _basic_market(),
        ),
        (
            "very_tight_spread",
            _make_book(0.50, 100.0, 0.501, 100.0),
            _basic_market(),
        ),
        (
            "very_wide_spread",
            _make_book(0.40, 100.0, 0.60, 100.0),
            _basic_market(),
        ),
        (
            "five_level_deep_book",
            OrderBook(
                token_id="TEST_TOKEN_S6_DEEP",
                bids=[PriceLevel(0.49 - i * 0.001, 50.0 * (i + 1)) for i in range(5)],
                asks=[PriceLevel(0.51 + i * 0.001, 50.0 * (i + 1)) for i in range(5)],
            ),
            _basic_market(),
        ),
        (
            "asymmetric_deep_book",
            OrderBook(
                token_id="TEST_TOKEN_S6_ASYM",
                bids=[PriceLevel(0.49 - i * 0.001, 200.0) for i in range(5)],
                asks=[PriceLevel(0.51 + i * 0.001, 10.0) for i in range(5)],
            ),
            _basic_market(),
        ),
    ],
)
def test_feature_vector_contains_no_nan_or_inf(label, book, market):
    vec = extract_features(market, book)
    if vec is None:
        pytest.skip(f"{label}: extract_features returned None (rejected) — nothing to check")
    assert vec.shape == (N_FEATURES,), f"{label}: bad shape {vec.shape}"
    nan_mask = np.isnan(vec)
    inf_mask = np.isinf(vec)
    assert not nan_mask.any(), (
        f"{label}: NaN in feature vector at indices {np.where(nan_mask)[0].tolist()}"
    )
    assert not inf_mask.any(), (
        f"{label}: Inf in feature vector at indices {np.where(inf_mask)[0].tolist()}"
    )
    # Belt-and-braces: every entry must be finite.
    assert np.isfinite(vec).all(), f"{label}: non-finite entries present in feature vector"


def test_feature_vector_no_nan_after_many_sequential_calls():
    """Simulate the rolling-history code path that the live poller exercises:
    extract_features called 60+ times for the same token (fills the
    `_price_history` deque). Verifies no NaN/Inf creeps in across the run
    (Hurst, momentum, and rolling-volatility features all depend on history)."""
    market = _basic_market()
    book = _make_book(bid_price=0.49, ask_price=0.51, bid_size=100.0, ask_size=100.0)
    for _ in range(65):
        vec = extract_features(market, book)
        assert vec is not None
        assert np.isfinite(vec).all(), "Non-finite entry appeared during repeated calls"
    assert vec.shape == (N_FEATURES,)
