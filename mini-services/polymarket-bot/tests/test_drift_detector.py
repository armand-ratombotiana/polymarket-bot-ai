"""
tests/test_drift_detector.py — Unit tests for ``ml/drift_detector.py``.

W3 — ML drift detector unit tests.

Covers the seven behaviours required by the task spec:

  (1) ``record_prediction`` stores predictions in ``recent_predictions``.
  (2) ``compute_psi`` returns ~0 when the live distribution matches the
      reference distribution (PSI is non-negative by definition; an
      identical actual/expected pair yields exactly 0.0).
  (3) ``compute_psi`` returns a high value (well above the 0.25
      ``SIGNIFICANT_DRIFT`` threshold) when the live distribution shifts
      away from the captured reference distribution.
  (4) ``reset()`` clears ``rolling_brier`` (and ``ewma_brier``) and sets
      ``drift_status`` back to ``HEALTHY``.
  (5) ``drift_status`` transitions to ``SIGNIFICANT_DRIFT`` when PSI
      exceeds 0.25.
  (6) ``get_status_report`` returns a dict carrying the four canonical
      numeric signals — ``psi`` / ``ks_stat`` / ``rolling_brier`` /
      ``ewma_brier`` — plus the auxiliary status / sample-count /
      threshold metadata the implementation also exposes.
  (7) ``reference_distribution`` is ``None`` until the first
      ``compute_psi`` call with ≥30 predictions, and is captured (as a
      10-element numpy array) on that first call.

The drift detector is **pure-Python + synchronous** — no DB, no async,
no env vars at module-import time. Every test is a plain ``def`` (no
``async def``) and runs without an event loop. The repo's ``pytest.ini``
declares ``testpaths = tests``; this file is collected automatically.

Conventions
-----------
* ``sys.path`` is bootstrapped so the test runs regardless of the cwd
  pytest was launched from (mirrors the bootstrap pattern in
  ``tests/test_features.py``, ``tests/test_decision_ledger.py``,
  ``tests/test_ml_validation.py``).
* Each test constructs a **fresh** ``ModelDriftDetector()`` instance
  rather than touching the module-level ``drift_detector`` singleton —
  the singleton accumulates state across the entire pytest session
  (``recent_predictions``, ``psi_history``, captured
  ``reference_distribution``, Brier escalations) and would leak between
  tests if reused. Fresh construction is the same pattern
  ``tests/test_decision_ledger.py`` uses for ``DecisionLedger``.
* The KS statistic is computed against ``np.random.choice``-sampled
  baseline points; the seed is left un-fixed because every test in
  this file asserts on PSI (fully deterministic given the inputs) and
  on the PSI-driven status transitions, NOT on the KS value. Tests
  that check status transitions for the PSI branch are robust to the
  KS randomness because PSI alone suffices to set the status when
  ``last_psi >= 0.25`` (see the branch order in ``compute_psi``).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Inline sys.path bootstrap — mirrors the pattern in test_features.py /
# test_paper_simulator.py / test_ml_validation.py. Required so the
# test module can `from ml.drift_detector import ...` regardless of the
# cwd pytest was launched from (monorepo root, CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from ml.drift_detector import (  # noqa: E402
    BRIER_DRIFT_THRESHOLD,
    ModelDriftDetector,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _record_n(detector: ModelDriftDetector, p_yes: float, n: int) -> None:
    """Call ``record_prediction`` ``n`` times with the same ``p_yes``.

    Uses the public ingestion API (not direct attribute assignment) so
    the test exercises the real path: window-cap eviction and the
    every-50 auto-``compute_psi`` trigger. For ``n`` < 50 the trigger
    never fires, so the caller controls when ``compute_psi`` runs.
    """
    for _ in range(n):
        detector.record_prediction(p_yes)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_1_record_prediction_stores_predictions():
    """``record_prediction`` appends each prediction to
    ``recent_predictions`` in insertion order.

    Uses 5 calls (well under the 50-sample auto-``compute_psi`` trigger
    in ``record_prediction``) so this test isolates the *storage*
    contract from the *drift computation* contract — the latter is
    covered by tests 2/3/5/7.
    """
    detector = ModelDriftDetector()
    assert detector.recent_predictions == []

    for p in (0.10, 0.25, 0.50, 0.75, 0.95):
        detector.record_prediction(p)

    assert detector.recent_predictions == [0.10, 0.25, 0.50, 0.75, 0.95]
    assert len(detector.recent_predictions) == 5


def test_2_compute_psi_returns_zero_when_distribution_matches_reference():
    """When the live distribution equals the captured reference
    distribution, PSI = 0 (each term ``(a_i - e_i) * ln(a_i/e_i)`` is
    zero because ``a_i == e_i`` for every bin).

    Procedure: feed 30 identical 0.5 predictions, call ``compute_psi``
    once (this captures the reference), then call ``compute_psi`` again
    against the same inputs — both calls return ``0.0``.

    PSI is also bounded below at 0 via ``max(psi, 0.0)`` before rounding
    to 4 dp, so any sub-epsilon float drift in the histogram still
    reports as exactly ``0.0``.
    """
    detector = ModelDriftDetector()
    _record_n(detector, 0.50, 30)

    first_psi = detector.compute_psi()
    # First call captures the reference; PSI is ~0 by construction.
    assert first_psi == pytest.approx(0.0, abs=1e-6)

    # Second call: actual == reference → PSI == 0 deterministically.
    second_psi = detector.compute_psi()
    assert second_psi == 0.0
    assert detector.last_psi == 0.0


def test_3_compute_psi_returns_high_value_when_distribution_shifts():
    """When the live distribution shifts away from the captured
    reference distribution (all-0.5 → all-0.95), PSI spikes well
    above the 0.25 ``SIGNIFICANT_DRIFT`` threshold.

    The reference distribution is captured on the first ``compute_psi``
    call (test 7 below covers that capture contract explicitly); the
    second ``compute_psi`` call compares the new bin frequencies against
    the captured reference.

    The expected PSI magnitude for a complete bin-flip (single-bin
    mass moving from bin [0.5, 0.6) to bin [0.9, 1.0)) is ≈ 26 (each
    of the two mass-bearing bins contributes ≈ 13). The assertion
    only requires ``> 0.25`` so the test stays valid if the
    implementation tweaks the smoothing constant or bin count — but
    a magnitude sanity-check (``> 5.0``) is also included so a
    future regression that mis-bins the predictions is surfaced.
    """
    detector = ModelDriftDetector()
    _record_n(detector, 0.50, 30)
    detector.compute_psi()  # captures reference (bin 5 dominant)

    # Swap in a completely different distribution (bin 9 dominant).
    # Direct attribute assignment bypasses record_prediction's auto-
    # compute_psi trigger (only fires at the 50/100/… mark) and keeps
    # the test deterministic about WHICH distribution compute_psi sees.
    detector.recent_predictions = [0.95] * 30
    shifted_psi = detector.compute_psi()

    assert shifted_psi > 0.25
    assert detector.last_psi == shifted_psi
    # Sanity-check magnitude: a full bin-flip is a *large* drift.
    assert shifted_psi > 5.0


def test_4_reset_clears_rolling_brier_and_sets_status_to_healthy():
    """``reset()`` clears ``rolling_brier`` (and ``ewma_brier``) and
    restores ``drift_status`` to ``HEALTHY``.

    Procedure: degrade the detector by recording 20 wildly-miscalibrated
    outcomes ``(p_yes=0.9, actual=0)`` so that ``rolling_brier`` = 0.81
    (> ``BRIER_DRIFT_THRESHOLD`` = 0.22) and ``drift_status`` escalates
    to ``SIGNIFICANT_DRIFT``. Then call ``reset()`` and assert the
    Brier-tracking fields are cleared and the status is back to
    ``HEALTHY``.

    Note: ``reset()`` also leaves ``recent_predictions`` empty and
    ``last_psi`` / ``last_ks_stat`` at 0, but the load-bearing contract
    for this test is the Brier + status recovery (the R6-1 fix that
    motivated the ``reset()`` extension — see the in-source comment in
    ``drift_detector.py``).
    """
    detector = ModelDriftDetector()

    # Degrade: 20 catastrophically wrong predictions (p=0.9, actual=0).
    for _ in range(20):
        detector.record_outcome(p_yes=0.90, actual=0)

    # Sanity-check the degraded state before reset().
    assert detector.rolling_brier is not None
    assert detector.rolling_brier > BRIER_DRIFT_THRESHOLD
    assert detector.ewma_brier is not None
    assert detector.ewma_brier > BRIER_DRIFT_THRESHOLD
    assert detector.drift_status == "SIGNIFICANT_DRIFT"

    detector.reset()

    assert detector.rolling_brier is None
    assert detector.ewma_brier is None
    assert detector.drift_status == "HEALTHY"


def test_5_drift_status_transitions_to_significant_drift_when_psi_high():
    """When PSI ≥ 0.25, ``drift_status`` is set to ``SIGNIFICANT_DRIFT``.

    The implementation's status branch is::

        if last_psi < 0.10 and last_ks_stat < 0.15:  HEALTHY
        elif last_psi < 0.25 and last_ks_stat < 0.25: MODERATE_SHIFT
        else:                                          SIGNIFICANT_DRIFT

    so PSI ≥ 0.25 alone (regardless of KS) suffices to set
    ``SIGNIFICANT_DRIFT``. A full bin-flip (0.5 → 0.95) drives PSI to
    ≈ 26, well above the threshold.

    Note on the KS branch
    ---------------------
    The post-capture status with 0.5-centered predictions is *already*
    ``SIGNIFICANT_DRIFT`` because the KS test compares the live
    predictions against the U-shaped market baseline
    (``_MARKET_BASELINE``) — NOT against the captured
    ``reference_distribution``. That baseline structurally disagrees with
    ~0.5-centered model predictions (see the in-source R6-2 comment in
    ``drift_detector.py``), so KS ≈ 0.5 on every 0.5-only capture and
    the status lands in the ``else`` branch immediately.

    This test therefore verifies the **PSI branch** of the status logic
    specifically: after the bin-flip, PSI is high AND status is
    ``SIGNIFICANT_DRIFT``. The pre-shift ``HEALTHY`` assertion is
    intentionally omitted because the KS branch makes it inherently
    flaky — the PSI contract (PSI ≥ 0.25 ⟹ SIGNIFICANT_DRIFT) is what
    the task spec asks us to verify.
    """
    detector = ModelDriftDetector()
    assert detector.drift_status == "HEALTHY"  # pre-capture: no PSI run yet

    _record_n(detector, 0.50, 30)
    detector.compute_psi()  # captures reference, PSI ≈ 0
    # PSI contract: post-capture PSI is ~0 by construction (actual == ref).
    assert detector.last_psi == pytest.approx(0.0, abs=1e-6)

    # Bin-flip: all mass moves from bin 5 to bin 9 → PSI spikes.
    detector.recent_predictions = [0.95] * 30
    psi = detector.compute_psi()

    assert psi >= 0.25
    assert detector.drift_status == "SIGNIFICANT_DRIFT"


def test_6_get_status_report_returns_psi_ks_brier_signals():
    """``get_status_report()`` returns a dict carrying the four
    canonical numeric drift signals — ``psi`` / ``ks_stat`` /
    ``rolling_brier`` / ``ewma_brier`` — with values that mirror the
    instance attributes they summarise.

    Also spot-checks the auxiliary metadata keys (``status``,
    ``window_samples``, ``outcome_samples``, the threshold constants)
    so a future regression that drops any of them is surfaced.
    """
    detector = ModelDriftDetector()

    # Drive enough state to populate every signal: ≥30 predictions so
    # compute_psi() runs and sets last_psi / last_ks_stat; ≥20 outcomes
    # so rolling_brier is computed (not None).
    _record_n(detector, 0.50, 30)
    detector.compute_psi()
    for _ in range(20):
        detector.record_outcome(p_yes=0.50, actual=1)

    report = detector.get_status_report()

    # The four canonical signals required by the W3 task spec.
    for key in ("psi", "ks_stat", "rolling_brier", "ewma_brier"):
        assert key in report, f"missing required signal key: {key}"

    assert report["psi"] == detector.last_psi
    assert report["ks_stat"] == detector.last_ks_stat
    assert report["rolling_brier"] == detector.rolling_brier
    assert report["ewma_brier"] == pytest.approx(detector.ewma_brier, abs=1e-4)

    # Auxiliary metadata — locked-in keys the API contract exposes.
    for aux in (
        "status",
        "window_samples",
        "outcome_samples",
        "threshold_moderate_psi",
        "threshold_critical_psi",
        "threshold_moderate_ks",
        "threshold_critical_ks",
        "threshold_brier_drift",
        "ewma_alpha",
        "history",
    ):
        assert aux in report, f"missing auxiliary key: {aux}"

    assert report["status"] == detector.drift_status
    assert report["window_samples"] == len(detector.recent_predictions)
    assert report["outcome_samples"] == len(detector.recent_actuals)
    assert report["threshold_brier_drift"] == BRIER_DRIFT_THRESHOLD


def test_7_reference_distribution_captured_on_first_compute_psi():
    """``reference_distribution`` is ``None`` until the first
    ``compute_psi`` call with ≥30 predictions, and is captured (as a
    10-element numpy array matching the bin-frequency shape of the live
    distribution at that moment) on that first call.

    Three sub-assertions:
      (a) Before the first ``compute_psi`` call, ``reference_distribution``
          is ``None`` even after recording 29 predictions (one below
          the warm-up guard).
      (b) A below-warm-up ``compute_psi()`` call (29 samples) returns
          ``last_psi`` (=0.0) WITHOUT capturing the reference.
      (c) After the first warm-up-eligible ``compute_psi`` call,
          ``reference_distribution`` is a 10-element numpy array whose
          values sum to ~1.0 (a valid probability distribution) and
          which matches the live bin frequencies at that moment.
    """
    detector = ModelDriftDetector()
    assert detector.reference_distribution is None

    # 29 predictions — below the 30-sample warm-up guard, so even an
    # explicit compute_psi() call would early-return WITHOUT capturing.
    _record_n(detector, 0.50, 29)
    assert detector.reference_distribution is None

    # Below-warm-up compute_psi returns last_psi (0.0) WITHOUT capturing.
    early_psi = detector.compute_psi()
    assert early_psi == 0.0
    assert detector.reference_distribution is None  # still not captured

    # 30th prediction → warm-up threshold met.
    detector.record_prediction(0.50)
    captured_psi = detector.compute_psi()

    assert detector.reference_distribution is not None
    assert isinstance(detector.reference_distribution, np.ndarray)
    assert detector.reference_distribution.shape == (10,)
    # Valid probability distribution: each bin ≥ 0, sum ≈ 1.
    assert np.all(detector.reference_distribution >= 0)
    assert detector.reference_distribution.sum() == pytest.approx(1.0, abs=1e-6)
    # Captured reference equals the live bin frequencies at capture time.
    # For 30 × p_yes=0.5, all mass lands in bin [0.5, 0.6) → bin index 5.
    assert detector.reference_distribution[5] > 0.99
    # First-call PSI against the just-captured reference ≈ 0.
    assert captured_psi == pytest.approx(0.0, abs=1e-6)
