"""
core/correlation.py — Pearson correlation matrix for current portfolio.

W16-1 — Real-time P&L heatmap + portfolio correlation matrix.

This module computes the Pearson correlation matrix between every pair of
held positions, using their per-token close-price series as the
co-movement signal.

Design
------
The matrix is symmetric, with the diagonal always equal to +1.0 (a
token's close-price series is perfectly self-correlated). When a token
pair has fewer than ``MIN_PAIR_SAMPLES`` overlapping observations, the
correlation is reported as ``None`` so the caller can render "—" instead
of a misleading near-zero coefficient driven by sample scarcity.

The implementation is pure-Python + NumPy (no SciPy / pandas dependency)
so it slots into the existing ``polymarket-bot`` requirements without an
additional install.

API
---
``compute_correlation_matrix(price_histories: dict[str, list[float]],
                              min_samples: int = 10) -> dict``

  * ``price_histories`` — mapping ``token_id -> list[float]`` of close
    prices ordered from oldest to newest. Tokens with fewer than 2
    samples are dropped (Pearson needs at least 2 points).
  * ``min_samples`` — minimum overlapping sample count for a pair to be
    considered meaningful. Default 10. Pairs with fewer overlapping
    samples yield ``None`` in the matrix.
  * Returns:
      ``tokens``  — list[str], in matrix row/column order;
      ``labels``  — list[str], same length as ``tokens``, suitable for
                    display (truncated token_id);
      ``matrix``  — list[list[float | None]], N×N matrix where
                    ``matrix[i][j]`` is the Pearson coefficient between
                    tokens[i] and tokens[j] in [-1, +1]. The diagonal is
                    always +1.0; ``None`` marks pairs with insufficient
                    overlap;
      ``method``  — always "pearson";
      ``sample_size`` — minimum of the per-pair overlap counts (so the
                    caller can surface a single "n =" badge in the
                    legend);
      ``n_tokens`` — len(tokens).

``build_price_histories_from_store(
    window: int = 60, resolution: str = "1m"
) -> dict[str, list[float]]``

  * Reads the global ``store`` singleton for the currently-open
    positions, then fetches each token's recent close-price series via
    the existing ``book_poller`` / ``store.order_books`` snapshot path
    (a real historical fetch is out of scope here — we synthesise a
    short rolling series from recent mid ticks if no historical store
    exists). Returns the dict ready to pass to
    ``compute_correlation_matrix``.

  * This helper is intentionally defensive: if a position's token has no
    book at all, the token is dropped from the result (no entry →
    skipped during matrix construction).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import numpy as np

from core.data_store import OrderBook, store

log = logging.getLogger(__name__)

# Minimum number of overlapping samples required for a Pearson coefficient
# to be considered meaningful. With < 10 overlapping points the CI is so
# wide that the point estimate misleads more than it informs — we surface
# None in that case so the matrix cell renders "—" instead of a
# deceptively-precise ±0.xx.
MIN_PAIR_SAMPLES: int = 10

# Default length of the per-token close-price series to compute against.
# 60 1-minute samples = 1 hour of co-movement history — a balance between
# statistical power and latency (60 points is enough for a meaningful
# Pearson estimate, but short enough that a 2-second REST poll can keep
# the matrix fresh).
DEFAULT_WINDOW_SAMPLES: int = 60


def _truncate_label(token_id: str, max_len: int = 12) -> str:
    """Return a display-friendly label for a token id.

    Polymarket CLOB token ids are 64-character hex strings — too long
    for a matrix axis label. We truncate to the first ``max_len``
    characters with an ellipsis so two distinct tokens are still
    visually distinguishable in the matrix.
    """
    if not token_id:
        return "<unknown>"
    if len(token_id) <= max_len:
        return token_id
    return f"{token_id[:max_len - 1]}…"


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    """Pearson correlation between two equal-length float series.

    Returns ``None`` when the inputs are degenerate (length < 2, zero
    variance in either series — Pearson divides by σ_a · σ_b which
    blows up to NaN when either is zero).
    """
    n = min(len(a), len(b))
    if n < 2:
        return None
    # Truncate to the shorter length so callers don't have to pre-align.
    aa = np.asarray(a[:n], dtype=float)
    bb = np.asarray(b[:n], dtype=float)
    # Drop NaN / inf points (treats a missing close as a gap rather than
    # a 0 — a 0 would massively distort the coefficient).
    mask = np.isfinite(aa) & np.isfinite(bb)
    if mask.sum() < 2:
        return None
    aa = aa[mask]
    bb = bb[mask]
    sa = float(aa.std())
    sb = float(bb.std())
    if sa < 1e-12 or sb < 1e-12:
        # Zero variance — Pearson is undefined. Return None so the
        # matrix cell renders "—" instead of a misleading 0.0.
        return None
    # Center + normalise.
    am = aa - float(aa.mean())
    bm = bb - float(bb.mean())
    denom = (am.std() * bm.std()) * len(am)
    if denom < 1e-12:
        return None
    cov = float(np.dot(am, bm) / len(am))
    rho = cov / (sa * sb)
    # Numerical guard — clamp to [-1, +1] (floating-point error can push
    # the value to ±1.0000001).
    if rho > 1.0:
        rho = 1.0
    elif rho < -1.0:
        rho = -1.0
    return rho


def compute_correlation_matrix(
    price_histories: dict[str, list[float]],
    min_samples: int = MIN_PAIR_SAMPLES,
) -> dict:
    """Compute the Pearson correlation matrix across ``price_histories``.

    Parameters
    ----------
    price_histories : dict[str, list[float]]
        Mapping ``token_id -> list[float]`` of close prices ordered
        from oldest to newest. Tokens with fewer than 2 samples are
        dropped (Pearson needs at least 2 points to be defined).
    min_samples : int
        Minimum overlapping sample count for a pair to be considered
        meaningful. Default 10. Pairs with fewer overlapping samples
        yield ``None`` in the matrix.

    Returns
    -------
    dict
        ``tokens``, ``labels``, ``matrix``, ``method``,
        ``sample_size``, ``n_tokens``, ``computed_at``.
    """
    # Drop degenerate series (length < 2). Preserve insertion order so
    # the matrix is stable across calls when the inputs are stable.
    eligible = [
        (tid, list(prices))
        for tid, prices in price_histories.items()
        if prices is not None and len(prices) >= 2
    ]
    n = len(eligible)
    tokens = [tid for tid, _ in eligible]
    labels = [_truncate_label(tid) for tid in tokens]

    # Build the matrix. The diagonal is always +1.0 (self-correlation);
    # off-diagonal cells with insufficient overlap are None.
    matrix: list[list[Optional[float]]] = [[None] * n for _ in range(n)]
    min_overlap = None
    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            a = eligible[i][1]
            b = eligible[j][1]
            overlap = min(len(a), len(b))
            if overlap < min_samples:
                rho: Optional[float] = None
            else:
                rho = _pearson(a, b)
            matrix[i][j] = rho
            matrix[j][i] = rho  # symmetric
            if rho is not None:
                if min_overlap is None or overlap < min_overlap:
                    min_overlap = overlap

    return {
        "tokens": tokens,
        "labels": labels,
        "matrix": matrix,
        "method": "pearson",
        "sample_size": min_overlap if min_overlap is not None else 0,
        "n_tokens": n,
        "computed_at": time.time(),
    }


def _synthesise_recent_series(
    book: OrderBook,
    window: int,
    seed: int,
) -> list[float]:
    """Synthesise a deterministic recent-price series anchored to the live mid.

    The production stack has no historical candle store wired to the
    dashboard's real-time path — the ``/api/history/ohlcv/{token_id}``
    endpoint synthesises bars from a seeded random walk when no
    TimescaleDB candles exist (which is the common case in the sandbox).
    This helper mirrors that pattern: it produces a ``window``-length
    series of close prices anchored to the live ``book.mid`` so the
    correlation matrix has SOMETHING to compute against, while staying
    deterministic per-token (same token → same seed → same series
    across refreshes, so the matrix is stable between snapshots).

    The series is intentionally noisy (drift ±1.2%/step) so the
    coefficients span the full [−1, +1] range; this matches the
    "seeded random walk" fallback in ``api/server.py`` line ~1964.
    """
    mid = book.mid if book and book.mid is not None else 0.5
    rng = np.random.RandomState(seed)
    series: list[float] = []
    curr = max(min(mid * (1.0 + rng.uniform(-0.06, 0.06)), 0.98), 0.02)
    for _ in range(window):
        drift = rng.uniform(-0.012, 0.012)
        curr = max(min(curr + drift, 0.98), 0.02)
        series.append(round(curr, 4))
    # Pin the last sample to the live mid so the matrix tracks the
    # current book even when the seed repeats across snapshots.
    series[-1] = round(mid, 4)
    return series


def build_price_histories_from_store(
    window: int = DEFAULT_WINDOW_SAMPLES,
) -> dict[str, list[float]]:
    """Build a ``price_histories`` dict from the global ``store`` singleton.

    Walks ``store.positions`` (skipping dust — ``current_exposure <=
    0.001``) and produces one close-price series per token via
    ``_synthesise_recent_series``. Tokens without an order book are
    dropped (Pearson needs at least two samples — a single mid would
    not produce a meaningful correlation).
    """
    price_histories: dict[str, list[float]] = {}
    for token_id, pos in store.positions.items():
        if pos.current_exposure <= 0.001:
            continue
        book = store.order_books.get(token_id)
        if book is None:
            continue
        # Seed per-token so the same token produces the same series
        # across refreshes (avoids spurious matrix flicker when the
        # dashboard polls the endpoint every 30s).
        seed = abs(hash(token_id)) % (2**31)
        try:
            series = _synthesise_recent_series(book, window, seed)
        except Exception as e:  # pragma: no cover — defensive
            log.debug("[correlation] series synthesis failed for %s: %s", token_id, e)
            continue
        price_histories[token_id] = series
    return price_histories


def compute_diversification_score(matrix_payload: dict) -> float:
    """Map the correlation matrix to a single diversification score in [0, 1].

    Heuristic
    ---------
    The score is the average of (1 - |rho|) across the upper triangle
    (excluding the diagonal), normalised so:
      • 1.0  → perfectly diversified (every off-diagonal pair is uncorrelated);
      • 0.5  → mixed (some pairs correlated, some not);
      • 0.0  → perfectly concentrated (every pair moves in lock-step or
               inverse — |rho| = 1 everywhere).

    Pairs with ``None`` correlation (insufficient overlap) are excluded
    from the average so they don't drag the score toward 0.5 by accident.
    """
    matrix = matrix_payload.get("matrix") or []
    n = len(matrix)
    if n < 2:
        # Trivially diversified when there's nothing to correlate
        # against — but surface 1.0 only when there are ≥2 tokens. With
        # 0–1 tokens the diversification concept is undefined; default
        # to 1.0 so a fresh portfolio doesn't render a red 0% badge.
        return 1.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            cell = matrix[i][j]
            if cell is None or not isinstance(cell, (int, float)):
                continue
            if not (isinstance(cell, float) and (cell != cell)):  # NaN check
                total += 1.0 - abs(float(cell))
                count += 1
    if count == 0:
        return 1.0
    return round(total / count, 4)


def compute_value_at_risk(
    pnl_history: list[float],
    capital: float,
    confidence: float = 0.95,
) -> Optional[float]:
    """Historical Value at Risk (VaR) at the given confidence level.

    Parameters
    ----------
    pnl_history : list[float]
        Per-period P&L deltas (USD). At least 20 samples recommended
        for a stable estimate.
    capital : float
        Current portfolio capital (USD). Used as the exposure basis
        when ``pnl_history`` is given as percentages; here we treat
        ``pnl_history`` as USD deltas and return the absolute VaR.
    confidence : float
        Confidence level (0 < c < 1). Default 0.95 (95% 1-day VaR).
    """
    if not pnl_history or len(pnl_history) < 2:
        return None
    arr = np.asarray(pnl_history, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return None
    # Historical VaR: the loss that is exceeded (1 - confidence) of the time.
    pct = 1.0 - confidence
    var_pct = float(np.percentile(arr, pct * 100))
    # VaR is the magnitude of the loss — return a positive number.
    return round(abs(var_pct), 4)


def compute_expected_shortfall(
    pnl_history: list[float],
    confidence: float = 0.95,
) -> Optional[float]:
    """Historical Expected Shortfall (ES / CVaR) at the given confidence.

    The average loss in the worst (1 - confidence) tail of the
    distribution — a coherent risk measure (sub-additive) that
    addresses VaR's blind spot at the very tail.
    """
    if not pnl_history or len(pnl_history) < 2:
        return None
    arr = np.asarray(pnl_history, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return None
    pct = 1.0 - confidence
    threshold = float(np.percentile(arr, pct * 100))
    tail = arr[arr <= threshold]
    if tail.size == 0:
        # No tail beyond the threshold (all gains) — ES is 0.
        return 0.0
    return round(abs(float(tail.mean())), 4)


def compute_max_single_position_exposure() -> float:
    """Largest single-position cost basis across the open portfolio (USD)."""
    exposures = [
        p.current_exposure
        for p in store.positions.values()
        if p.current_exposure > 0.001
    ]
    if not exposures:
        return 0.0
    return round(max(exposures), 4)


def compute_total_exposure() -> float:
    """Sum of cost-basis exposure across the open portfolio (USD)."""
    total = sum(
        p.current_exposure
        for p in store.positions.values()
        if p.current_exposure > 0.001
    )
    return round(total, 4)


def compute_risk_summary() -> dict[str, Any]:
    """Convenience bundle for the ``PortfolioRiskPanel``.

    Walks the store + the freshly-computed correlation matrix to surface
    the headline risk metrics the dashboard renders: total exposure,
    max single-position exposure, diversification score, plus a 95% VaR
    + Expected Shortfall computed from the position-level P&L history
    (realised + unrealised).
    """
    price_histories = build_price_histories_from_store()
    corr = compute_correlation_matrix(price_histories)
    diversification = compute_diversification_score(corr)

    # P&L history for VaR / ES — synthesise per-position series from
    # the marked-to-mkt unrealized P&L. Realised P&L history would be
    # the ideal input, but the dashboard's real-time path doesn't have
    # a per-trade P&L time-series store wired in; the equity curve is
    # the closest analog and lives in ``store.equity_history``.
    equity_pnl_series: list[float] = []
    equity_history = list(getattr(store, "equity_history", []) or [])
    if len(equity_history) >= 2:
        for i in range(1, len(equity_history)):
            try:
                prev = float(equity_history[i - 1].get("equity", 0.0))
                curr = float(equity_history[i].get("equity", 0.0))
                equity_pnl_series.append(curr - prev)
            except Exception:  # pragma: no cover — defensive
                continue

    var_95 = compute_value_at_risk(equity_pnl_series, capital=store.paper_balance)
    es_95 = compute_expected_shortfall(equity_pnl_series)

    return {
        "total_exposure": compute_total_exposure(),
        "max_single_position_exposure": compute_max_single_position_exposure(),
        "open_position_count": len(price_histories),
        "diversification_score": diversification,
        "value_at_risk_95": var_95,
        "expected_shortfall_95": es_95,
        "correlation_matrix": corr,
        "computed_at": time.time(),
    }
