"""
W9-5 — Unit tests for ``core/deep_analysis.py``.

Covers the Deep Market Analysis engine's public surface:

  1. ``record_whale_trade`` returns ``None`` when the trade size_usdc is
     below the $5,000 threshold (not a whale).
  2. ``record_whale_trade`` returns a ``WhaleActivity`` and appends to
     ``whale_alerts`` when size_usdc >= $5,000.
  3. ``record_whale_trade`` caps ``whale_alerts`` at 50 entries — the
     ``self.whale_alerts[:50]`` trim.
  4. ``record_whale_trade`` at the EXACT $5,000 boundary IS a whale (>=).
  5. ``record_whale_trade`` looks up the slug from ``store.market_slugs``
     when present, falling back to ``token_id[:14]``.
  6. ``classify_regime`` returns ``Resolution Convergence`` when mid is
     >= 0.92 OR <= 0.08.
  7. ``classify_regime`` returns ``High Volatility`` when spread >= 0.04.
  8. ``classify_regime`` returns ``Directional Trending`` when depth
     imbalance > 0.4.
  9. ``classify_regime`` returns ``Mean-Reverting Range`` when no other
     trigger fires (the default branch).
 10. ``classify_regime`` handles None mid / spread gracefully (defaults
     to 0.5 / 0.01).
 11. ``get_category_correlation_matrix`` returns the canonical 5x5
     correlation matrix with 5 categories.
 12. ``WhaleActivity.to_dict`` echoes every public field and rounds
     ``size_usdc`` to 2dp.

Isolation
----------
Each test constructs a FRESH ``DeepMarketAnalysisEngine()`` instance and
seeds ``store.market_slugs`` directly (the autouse conftest fixture
resets ``store`` to a clean baseline before every test).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
even though these tests are sync, the mark is harmless and keeps
collection consistent).
"""
from __future__ import annotations

import pytest

from core.data_store import OrderBook, PriceLevel, store
from core.deep_analysis import (
    DeepMarketAnalysisEngine,
    WhaleActivity,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def engine():
    """Fresh ``DeepMarketAnalysisEngine`` per test.

    The engine's module-level singleton ``deep_analysis_engine`` ships with
    three demo seed whale_alerts (see the bottom of ``core/deep_analysis.py``);
    the fresh instance starts with an EMPTY whale_alerts list.
    """
    return DeepMarketAnalysisEngine()


def _book(
    mid_target: float,
    spread: float = 0.01,
    bid_size: float = 100.0,
    ask_size: float = 100.0,
    token_id: str = "TOK_X",
) -> OrderBook:
    """Construct an ``OrderBook`` with a deterministic ``mid`` and ``spread``
    plus single-level bid/ask ladders of the specified sizes (used to drive
    the depth-imbalance computation in ``classify_regime``)."""
    best_bid = mid_target - spread / 2.0
    best_ask = mid_target + spread / 2.0
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=best_bid, size=bid_size)],
        asks=[PriceLevel(price=best_ask, size=ask_size)],
    )


# ── 1. record_whale_trade below threshold returns None ────────────────────────
def test_record_whale_trade_below_threshold_returns_none(engine):
    """A trade whose notional (price * size_shares) is below $5,000 must
    return ``None`` — the $5,000 whale threshold."""
    # price 0.50 × size 1000 shares = $500 — well below $5,000.
    result = engine.record_whale_trade("TOK_X", side="BUY", price=0.50, size_shares=1000.0)
    assert result is None
    assert len(engine.whale_alerts) == 0


# ── 2. record_whale_trade above threshold returns WhaleActivity ──────────────
def test_record_whale_trade_above_threshold_returns_whale_activity(engine):
    """A trade whose notional >= $5,000 returns a ``WhaleActivity`` and is
    prepended to ``whale_alerts`` (newest-first)."""
    # price 0.50 × size 10000 shares = $5,000 — exactly the threshold.
    result = engine.record_whale_trade("TOK_X", side="BUY", price=0.50, size_shares=10_000.0)
    assert result is not None
    assert isinstance(result, WhaleActivity)
    assert result.token_id == "TOK_X"
    assert result.side == "BUY"
    assert result.price == pytest.approx(0.50)
    assert result.size_usdc == pytest.approx(5000.0)
    assert len(engine.whale_alerts) == 1
    assert engine.whale_alerts[0] is result


# ── 3. record_whale_trade caps whale_alerts at 50 ─────────────────────────────
def test_record_whale_trade_caps_whale_alerts_at_50(engine):
    """After 50 entries, the list is trimmed to 50 — the documented cap."""
    # Record 52 whale trades; only the 50 most-recent must remain.
    for i in range(52):
        engine.record_whale_trade(
            f"TOK_{i}", side="BUY", price=0.50, size_shares=10_000.0,
        )
    assert len(engine.whale_alerts) == 50
    # Newest is TOK_51 (the last recorded); oldest retained is TOK_2.
    assert engine.whale_alerts[0].token_id == "TOK_51"
    assert engine.whale_alerts[-1].token_id == "TOK_2"


# ── 4. record_whale_trade at the EXACT $5,000 boundary IS a whale ─────────────
def test_record_whale_trade_at_exact_threshold_is_whale(engine):
    """``size_usdc >= 5000.0`` — the boundary value $5,000 itself qualifies
    as a whale trade (NOT strictly greater)."""
    # 0.50 × 10000 = $5,000 exactly.
    result = engine.record_whale_trade("TOK_X", side="BUY", price=0.50, size_shares=10000.0)
    assert result is not None
    assert result.size_usdc == pytest.approx(5000.0)


# ── 5. record_whale_trade looks up slug from store.market_slugs ────────────────
def test_record_whale_trade_uses_slug_from_store_when_present(engine):
    """When ``store.market_slugs[token_id]`` is set, the whale activity's
    slug must come from the store; otherwise the fallback is ``token_id[:14]``."""
    # Seed the store with a slug for TOK_KNOWN.
    store.market_slugs["TOK_KNOWN"] = "bitcoin-100k-2026"
    # Whale trade on TOK_KNOWN → slug is looked up.
    known = engine.record_whale_trade("TOK_KNOWN", side="BUY", price=0.50, size_shares=10_000.0)
    assert known is not None
    assert known.slug == "bitcoin-100k-2026"

    # Whale trade on TOK_UNKNOWN (not in store.market_slugs) → fallback to
    # the first 14 characters of the token id.
    long_token = "TOK_UNKNOWN_WITH_LONG_NAME_EXCEEDING_14_CHARS"
    unknown = engine.record_whale_trade(long_token, side="BUY", price=0.50, size_shares=10_000.0)
    assert unknown is not None
    assert unknown.slug == long_token[:14]


# ── 6. classify_regime returns Resolution Convergence near extremes ───────────
def test_classify_regime_resolution_convergence_at_high_mid(engine):
    """When ``mid >= 0.92`` OR ``mid <= 0.08``, the regime is
    ``Resolution Convergence`` — the near-certain outcome branch."""
    # mid = 0.93 (>= 0.92).
    book_high = _book(mid_target=0.93, spread=0.02, bid_size=100, ask_size=100)
    out_high = engine.classify_regime(book_high)
    assert out_high["regime"] == "Resolution Convergence"
    assert out_high["tag"] == "resolution"
    assert out_high["volatility"] == "Low"

    # mid = 0.07 (<= 0.08).
    book_low = _book(mid_target=0.07, spread=0.02, bid_size=100, ask_size=100)
    out_low = engine.classify_regime(book_low)
    assert out_low["regime"] == "Resolution Convergence"


# ── 7. classify_regime returns High Volatility when spread >= 0.04 ────────────
def test_classify_regime_high_volatility_when_spread_wide(engine):
    """When ``spread >= 0.04`` (and mid is NOT in the resolution band), the
    regime is ``High Volatility / Wide Spread``."""
    # mid = 0.50 (NOT resolution), spread = 0.06 (>= 0.04).
    book = _book(mid_target=0.50, spread=0.06, bid_size=100, ask_size=100)
    out = engine.classify_regime(book)
    assert out["regime"] == "High Volatility / Wide Spread"
    assert out["tag"] == "volatile"
    assert out["volatility"] == "High"


# ── 8. classify_regime returns Directional Trending when OFI > 0.4 ───────────
def test_classify_regime_directional_trending_when_ofi_gt_0_4(engine):
    """When depth imbalance > 0.4 (and mid is NOT resolution + spread < 0.04),
    the regime is ``Directional Trending``."""
    # mid = 0.50, spread = 0.01 (tight, NOT high-vol).
    # depth_imb = |bid_sz - ask_sz| / (bid_sz + ask_sz)
    # = |1000 - 100| / 1100 = 900/1100 = 0.818 > 0.4 ✓
    book = _book(mid_target=0.50, spread=0.01, bid_size=1000.0, ask_size=100.0)
    out = engine.classify_regime(book)
    assert out["regime"] == "Directional Trending"
    assert out["tag"] == "trending"
    assert out["volatility"] == "Medium"


# ── 9. classify_regime returns Mean-Reverting Range as default ─────────────────
def test_classify_regime_mean_reverting_range_as_default(engine):
    """When no trigger fires (mid NOT in resolution band, spread < 0.04,
    depth imbalance <= 0.4), the regime is the default
    ``Mean-Reverting Range``."""
    # mid = 0.50, spread = 0.01, balanced sizes → depth_imb = 0.
    book = _book(mid_target=0.50, spread=0.01, bid_size=100.0, ask_size=100.0)
    out = engine.classify_regime(book)
    assert out["regime"] == "Mean-Reverting Range"
    assert out["tag"] == "mean_reverting"
    assert out["volatility"] == "Low"


# ── 10. classify_regime handles None mid / spread gracefully ──────────────────
def test_classify_regime_handles_none_mid_and_spread(engine):
    """When ``book.mid`` is None and ``book.spread`` is None (empty book),
    the regime classifier must fall back to mid=0.5 / spread=0.01 defaults
    and not crash."""
    empty_book = OrderBook(token_id="TOK_EMPTY", bids=[], asks=[])
    out = engine.classify_regime(empty_book)
    # Empty book: mid defaults to 0.5, spread defaults to 0.01, depth_imb=0/0
    # guarded by max(...,1.0) → 0 → falls into Mean-Reverting default.
    assert out["regime"] == "Mean-Reverting Range"
    assert out["tag"] == "mean_reverting"


# ── 11. get_category_correlation_matrix returns canonical 5x5 ────────────────
def test_get_category_correlation_matrix_returns_canonical_5x5(engine):
    """The correlation matrix must be 5x5 with the canonical 5 categories,
    each row's diagonal == 1.00 (self-correlation), and the matrix is
    symmetric (matrix[i][j] == matrix[j][i])."""
    out = engine.get_category_correlation_matrix()
    categories = out["categories"]
    matrix = out["matrix"]

    assert len(categories) == 5
    assert categories == ["Crypto", "Macro & Rates", "Politics & Elections", "Sports", "Tech & AI"]

    assert len(matrix) == 5
    assert all(len(row) == 5 for row in matrix)

    # Diagonal is 1.00.
    for i in range(5):
        assert matrix[i][i] == 1.00

    # Symmetric.
    for i in range(5):
        for j in range(5):
            assert matrix[i][j] == matrix[j][i]


# ── 12. WhaleActivity.to_dict echoes fields and rounds size_usdc ─────────────
def test_whale_activity_to_dict_rounds_size_usdc():
    """``WhaleActivity.to_dict`` echoes every public field and rounds
    ``size_usdc`` to 2dp."""
    import time
    activity = WhaleActivity(
        token_id="TOK_X",
        slug="market-x",
        side="BUY",
        price=0.6234,
        size_usdc=12345.6789,  # rounds to 12345.68
        timestamp=1700000000.0,
    )
    d = activity.to_dict()
    assert d["token_id"] == "TOK_X"
    assert d["slug"] == "market-x"
    assert d["side"] == "BUY"
    assert d["price"] == 0.6234
    assert d["size_usdc"] == pytest.approx(12345.68, abs=1e-2)
    assert d["timestamp"] == 1700000000.0
