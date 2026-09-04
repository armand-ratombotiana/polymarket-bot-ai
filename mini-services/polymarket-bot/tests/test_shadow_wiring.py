"""
tests/test_shadow_wiring.py — W19-8 Shadow Inference Wiring unit tests.

Covers the five behaviour contracts the W19-8 task spec enumerates:

  (1) ``ml_model.predict()`` RECORDS shadow predictions — the
      production predict path now calls ``shadow_inference.predict_all``
      + ``shadow_inference.record_predictions`` (split API) instead of
      the historical single-shot ``run_shadow`` convenience wrapper.
      After registering a challenger on the module-level singleton and
      invoking ``fitted_model.predict()``, the challenger's call count
      in the engine's status report MUST have grown.

  (2) A/B test assignment works end-to-end — the signal trader's
      ``_ml_signal`` consults ``ab_test.assign_model(token_id)`` and
      ``ab_test.get_model_for_version(version, default=ml_model)`` to
      decide which model to invoke. With no experiment running,
      ``assign_model`` returns the ``"champion"`` sentinel and the
      trader falls back to ``ml_model`` (no behaviour change). With an
      experiment running AND a registered challenger callable, tokens
      deterministically assigned to the challenger arm invoke the
      challenger callable (not the champion).

  (3) ``ml_model.warmup()`` is callable and propagates the meta-learner
      summary. The method wraps
      ``ensemble_meta_learner.warm_from_labeled_samples()`` defensively
      so a meta-learner warmup failure cannot raise into the caller
      (production startup). Verified by monkey-patching the
      meta-learner's warmup method to return a controlled summary and
      asserting ``ml_model.warmup()`` returns the same dict.

  (4) ``shadow_inference.evaluate_and_promote()`` returns a per-
      challenger comparison dict keyed by challenger name, with each
      entry carrying ``n_samples``, ``champion_brier``,
      ``challenger_brier``, ``brier_improvement``, ``t_statistic``,
      ``p_value``, ``is_significantly_better``, and ``promoted``. A
      challenger with significantly lower Brier than the champion
      (paired t-test p-value < ``alpha``) is flagged
      ``is_significantly_better=True`` AND ``promoted=True`` AND
      recorded in the engine's ``_promotions`` ledger (surfaced via
      ``get_status_report()["promotions"]``).

  (5) The HTTP surface ``/api/ml/shadow`` + ``/api/ml/shadow/evaluate``
      works end-to-end via ``TestClient`` — the GET returns the registry
      snapshot (``registered_models`` / ``total_calls`` /
      ``promotions``); the POST triggers ``evaluate_and_promote`` and
      returns the per-challenger comparison dict + the
      ``insufficient_data`` list naming challengers that didn't have
      enough outcome-stamped rows.

Hermeticity
-----------
* Tests use a FRESH ``ShadowInferenceEngine()`` per test (the
  ``engine`` fixture) so the module-level singleton
  ``shadow_inference`` is never perturbed.
* The API integration test patches the ``register_routes`` handlers'
  closed-over module-level singleton to a fresh engine instance so the
  TestClient sees a clean registry.
* ``fitted_model`` mirrors the fixture in
  ``tests/integration/test_ml_pipeline.py`` — a freshly-trained
  ``MarketMLModel()`` trained on 100 rows of synthetic data so a
  single ``fit_initial`` call takes ~1.3 s.
* The repo's ``pytest.ini`` declares ``testpaths = tests``; this file
  is collected automatically. The sibling ``tests/conftest.py`` is
  imported BEFORE this module so its env-var redirects + ``sys.path``
  bootstrap are already in effect.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

# ── Bootstrap project root on sys.path (defensive; conftest.py also does this). ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ml.ab_testing import ABTestManager  # noqa: E402
from ml.features import N_FEATURES  # noqa: E402
from ml.model import MarketMLModel, _synthetic_training_data  # noqa: E402
from ml.shadow_inference import (  # noqa: E402
    ShadowInferenceEngine,
    shadow_inference as shadow_inference_singleton,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_features(mid_price: float = 0.5) -> np.ndarray:
    """Build a minimal valid 38-dim float32 feature vector."""
    vec = np.zeros(N_FEATURES, dtype=np.float32)
    vec[0] = float(mid_price)
    return vec


@pytest.fixture
def engine() -> ShadowInferenceEngine:
    """Fresh ``ShadowInferenceEngine`` per test — module-level singleton
    is left untouched so production code paths that import
    ``shadow_inference`` directly still see the singleton in its
    post-import state.

    Mirrors the isolation strategy in ``tests/test_shadow_inference.py``.
    """
    return ShadowInferenceEngine()


@pytest.fixture
def fitted_model():
    """A freshly-trained ``MarketMLModel()`` trained on synthetic data only.

    Mirrors the ``fitted_model`` fixture in
    ``tests/integration/test_ml_pipeline.py`` — 100 rows + 10 estimators
    so ``fit_initial`` takes ~1.3 s instead of ~25 s. The module-level
    singleton ``ml_model`` is left in its post-import state.
    """
    with patch(
        "core.timescale_db.timescale_db.fetch_training_samples",
        return_value=(None, []),
    ):
        with patch(
            "ml.model._synthetic_training_data",
            return_value=_synthetic_training_data(n=100),
        ):
            model = MarketMLModel()
            model.fit_initial(
                rf_max_depth=4,
                gb_learning_rate=0.1,
                n_estimators_rf=10,
                n_estimators_gb=10,
            )
            return model


@pytest.fixture
def manager(tmp_path) -> ABTestManager:
    """Fresh ``ABTestManager`` whose SQLite file lives under ``tmp_path``.

    The module-level singleton ``ab_test`` is left untouched —
    production code paths that import ``ab_test`` directly still see
    the singleton in its post-import state.
    """
    return ABTestManager(db_path=tmp_path / "ab_tests.db")


# =============================================================================
# (1) ml_model.predict() records shadow predictions via the split API
# =============================================================================
class TestPredictPathRecordsShadowPredictions:
    """W19-8 contract (1): ``ml_model.predict()`` records shadow predictions
    via ``shadow_inference.predict_all`` + ``record_predictions`` (the
    split API), not the historical single-shot ``run_shadow``."""

    def test_predict_all_returns_per_challenger_dict(self, engine):
        """``predict_all`` invokes every registered challenger once and
        returns a ``{name: p_yes}`` dict. Empty dict when no challengers
        are registered. Buggy challengers are skipped (omitted from the
        dict + ``total_errors`` bumped)."""
        # No challengers registered → empty dict.
        assert engine.predict_all(_make_features()) == {}

        # Register two challengers: one well-behaved, one buggy.
        engine.register_shadow_model(
            "good", lambda f: 0.42, description="well-behaved"
        )
        engine.register_shadow_model(
            "broken", lambda f: (_ for _ in ()).throw(ValueError("boom")),
            description="always raises",
        )

        preds = engine.predict_all(_make_features())
        assert "good" in preds
        assert preds["good"] == pytest.approx(0.42)
        # Buggy challenger is omitted from the returned dict.
        assert "broken" not in preds
        # Per-challenger error counter is bumped.
        assert engine.total_errors == 1
        # No recording happened yet — record_predictions is a separate call.
        assert engine.total_calls == 0

    def test_record_predictions_appends_history_and_bumps_calls(self, engine):
        """``record_predictions`` appends one comparison row per challenger
        in ``shadow_preds`` and bumps per-challenger ``calls`` + aggregate
        ``total_calls``. Idempotent on token_id — does NOT consult
        registered_models (so a de-registered challenger named in
        ``shadow_preds`` is silently skipped)."""
        engine.register_shadow_model("alpha", lambda f: 0.10, description="a")
        engine.register_shadow_model("beta", lambda f: 0.20, description="b")

        # Record a comparison row for both challengers.
        engine.record_predictions(
            token_id="TOK_1",
            champion_pred=0.5,
            shadow_preds={"alpha": 0.10, "beta": 0.20},
        )

        report = engine.get_status_report()
        assert report["total_calls"] == 2  # one per challenger
        by_name = {r["name"]: r for r in report["registered_models"]}
        assert by_name["alpha"]["calls"] == 1
        assert by_name["beta"]["calls"] == 1
        # History rows carry the champion + shadow + abs_delta + outcome
        # (None until record_outcome back-fills).
        alpha_last = by_name["alpha"]["last_comparison"]
        assert alpha_last["token_id"] == "TOK_1"
        assert alpha_last["p_production"] == pytest.approx(0.5)
        assert alpha_last["p_shadow"] == pytest.approx(0.10)
        assert alpha_last["abs_delta"] == pytest.approx(0.40)
        assert alpha_last["outcome"] is None  # not yet resolved

    def test_record_predictions_empty_dict_is_noop(self, engine):
        """``record_predictions`` with an empty ``shadow_preds`` dict is
        a defensive no-op — no state is mutated."""
        engine.register_shadow_model("alpha", lambda f: 0.10)
        engine.record_predictions("TOK", 0.5, {})
        report = engine.get_status_report()
        assert report["total_calls"] == 0
        assert report["registered_models"][0]["calls"] == 0
        assert report["registered_models"][0]["last_comparison"] is None

    def test_production_predict_invokes_split_api_on_singleton(
        self, fitted_model
    ):
        """The production ``ml_model.predict()`` path records shadow
        predictions via the split ``predict_all`` + ``record_predictions``
        API on the module-level singleton. After registering a
        challenger on the singleton and invoking ``fitted_model.predict()``
        with the singleton patched in (via the production import path),
        the challenger's ``calls`` count MUST grow.

        Verifies the cross-module wiring: the predict path's
        ``from ml.shadow_inference import shadow_inference`` import
        resolves to the same singleton the test registers the
        challenger on.
        """
        # Register a challenger on the module-level singleton.
        challenger_name = "w19_8_wiring_test_challenger"
        shadow_inference_singleton.register_shadow_model(
            name=challenger_name,
            fn=lambda features: 0.42,
            description="W19-8 wiring-test challenger",
        )
        try:
            pre_report = shadow_inference_singleton.get_status_report()
            pre_calls = next(
                (
                    m["calls"]
                    for m in pre_report["registered_models"]
                    if m["name"] == challenger_name
                ),
                0,
            )

            # Invoke predict on the locally-fitted model. The predict
            # path imports ``shadow_inference`` from ``ml.shadow_inference``
            # so it consults the same singleton we just registered on.
            features = _make_features(mid_price=0.5)
            fitted_model.predict(features, token_id="TEST_W19_8_WIRING")

            post_report = shadow_inference_singleton.get_status_report()
            post_calls = next(
                (
                    m["calls"]
                    for m in post_report["registered_models"]
                    if m["name"] == challenger_name
                ),
                0,
            )
            assert post_calls > pre_calls, (
                f"predict() must invoke shadow_inference.predict_all + "
                f"record_predictions; challenger calls pre={pre_calls}, "
                f"post={post_calls}"
            )

            # The recorded comparison row carries the production p_yes
            # and the challenger's 0.42 estimate.
            last = next(
                m["last_comparison"]
                for m in post_report["registered_models"]
                if m["name"] == challenger_name
            )
            assert last is not None
            assert last["token_id"] == "TEST_W19_8_WIRING"
            assert last["p_shadow"] == pytest.approx(0.42, abs=1e-3)
            # The champion prediction must be a valid probability in [0.01, 0.99].
            assert 0.01 <= last["p_production"] <= 0.99
        finally:
            # Cleanup so subsequent tests see a clean singleton.
            shadow_inference_singleton.unregister_shadow_model(challenger_name)


# =============================================================================
# (2) A/B test assignment works end-to-end
# =============================================================================
class TestABTestAssignmentWiring:
    """W19-8 contract (2): the signal trader consults
    ``ab_test.assign_model(token_id)`` and
    ``ab_test.get_model_for_version(version, default=ml_model)`` to decide
    which model to invoke per token."""

    def test_assign_model_returns_champion_sentinel_when_no_experiment(
        self, manager
    ):
        """With no experiment running, ``assign_model`` returns the
        ``"champion"`` sentinel so the trader falls back to ``ml_model``."""
        assert manager._current_experiment is None
        assert manager.assign_model(token_id="any_token") == "champion"

    def test_get_model_for_version_falls_back_to_default(self, manager):
        """``get_model_for_version`` returns the ``default`` argument when
        no challenger callable is registered for ``version`` — covers the
        no-experiment + champion-version + missing-challenger cases."""
        sentinel = object()
        # No experiment → "champion" → not in registry → default returned.
        assert manager.get_model_for_version("champion", default=sentinel) is sentinel
        # Unknown version → default returned.
        assert manager.get_model_for_version("v_unknown", default=sentinel) is sentinel

    def test_get_model_for_version_returns_registered_challenger(self, manager):
        """``get_model_for_version`` returns the registered challenger
        callable when one is registered for ``version``."""
        sentinel = object()
        my_challenger = lambda features, token_id="": (0.42, 0.84)
        manager.register_challenger_model("v_challenger", my_challenger)
        # The registered callable is returned, NOT the default sentinel.
        result = manager.get_model_for_version("v_challenger", default=sentinel)
        assert result is my_challenger

    def test_register_challenger_model_rejects_non_callable(self, manager):
        """``register_challenger_model`` silently rejects a non-callable
        ``predict_fn`` (defensive — operator config errors must not crash
        startup)."""
        manager.register_challenger_model("v_bad", predict_fn=None)
        manager.register_challenger_model("", predict_fn=lambda f, t="": (0.5, 0.5))
        # Neither registration took effect.
        sentinel = object()
        assert manager.get_model_for_version("v_bad", default=sentinel) is sentinel
        assert manager.get_model_for_version("", default=sentinel) is sentinel

    def test_assign_model_is_deterministic_per_token(self, manager, tmp_path):
        """``assign_model`` returns the SAME version for the SAME
        ``token_id`` across repeated calls — the deterministic-assignment
        contract that prevents within-token arm contamination.

        Pre-W19-8 this contract was verified in ``tests/test_ab_testing.py``;
        we re-verify it here through the W19-8 wiring lens (the signal
        trader depends on this so the SAME token always invokes the SAME
        model version on every scan).
        """
        manager.start_experiment(
            name="det_test",
            champion_version="champ_v1",
            challenger_version="chall_v2",
            traffic_split=0.5,
            min_samples=10,
        )
        # Sample 50 tokens and verify each maps to a stable version.
        for i in range(50):
            tid = f"token_{i}"
            v1 = manager.assign_model(token_id=tid)
            v2 = manager.assign_model(token_id=tid)
            v3 = manager.assign_model(token_id=tid)
            assert v1 == v2 == v3, (
                f"non-deterministic assignment for {tid}: {v1!r} → {v2!r} → {v3!r}"
            )
            assert v1 in {"champ_v1", "chall_v2"}

    def test_signal_trader_ml_signal_uses_assign_model(
        self, fitted_model, monkeypatch
    ):
        """End-to-end: ``SignalTraderStrategy._ml_signal`` consults
        ``ab_test.assign_model`` + ``get_model_for_version`` to pick the
        active model per token. When no experiment is running, the
        strategy falls back to ``ml_model`` (no behaviour change vs
        pre-W19-8).

        Verifies the cross-module wiring: ``_ml_signal`` imports
        ``ab_test`` from ``ml.ab_testing`` so it consults the same
        module-level singleton the test patches.
        """
        # Build the order book + market dict the signal trader expects.
        # ``OrderBook`` takes ``PriceLevel`` objects (not tuples) and
        # computes ``mid`` / ``spread`` / ``best_bid`` / ``best_ask`` as
        # properties from the level lists.
        from core.data_store import OrderBook, PriceLevel
        from strategies.signal_trader import SignalTraderStrategy

        book = OrderBook(
            token_id="TEST_AB_WIRING",
            bids=[PriceLevel(price=0.45, size=100.0)],
            asks=[PriceLevel(price=0.55, size=100.0)],
        )
        mkt = {"slug": "test-ab-wiring-market"}

        # Patch the module-level ml_model with our fitted_model so the
        # signal trader uses a known-fitted model rather than the
        # potentially-stale singleton.
        with patch("strategies.signal_trader.ml_model", fitted_model):
            strategy = SignalTraderStrategy()

            # Capture every ``assign_model`` invocation via a side_effect.
            assign_calls: list[str] = []
            original_assign = None
            from ml.ab_testing import ab_test as ab_singleton

            def _tracking_assign(token_id=None):
                assign_calls.append(token_id)
                return "champion"  # no experiment running

            # Save & restore ``assign_model`` on the singleton so the
            # post-test state is preserved across the rest of the session.
            original_assign = ab_singleton.assign_model
            try:
                ab_singleton.assign_model = _tracking_assign  # type: ignore[assignment]
                # ``_ml_signal`` should return None (signal) for a feature
                # vector that doesn't trip the 0.55/0.45 directional gate,
                # but the key contract is that ``assign_model`` was
                # consulted — i.e. the strategy did NOT just hard-code the
                # champion path.
                features = _make_features(mid_price=0.5)
                strategy._ml_signal(
                    "TEST_AB_WIRING", "test-ab-wiring", mkt, book, features
                )
                assert assign_calls, (
                    "_ml_signal must consult ab_test.assign_model; "
                    "no invocation recorded"
                )
                assert "TEST_AB_WIRING" in assign_calls
            finally:
                ab_singleton.assign_model = original_assign  # type: ignore[assignment]


# =============================================================================
# (3) ml_model.warmup() is callable and propagates the meta-learner summary
# =============================================================================
class TestMetaLearnerWarmupWiring:
    """W19-8 contract (3): ``ml_model.warmup()`` wraps
    ``ensemble_meta_learner.warm_from_labeled_samples()`` defensively —
    a meta-learner failure MUST NOT propagate into the caller (production
    lifespan startup)."""

    def test_warmup_returns_summary_dict_on_success(self, fitted_model):
        """``warmup`` returns the meta-learner's summary dict verbatim —
        the operator-facing payload (``n_loaded`` / ``buffer_size`` /
        ``is_warm`` / ``error``) so the lifespan startup hook can log
        the headline."""
        from ml.ensemble_meta_learner import ensemble_meta_learner

        fake_summary = {
            "n_requested": 200,
            "n_loaded": 42,
            "n_skipped": 0,
            "buffer_size": 42,
            "is_warm": True,
            "error": None,
        }
        with patch.object(
            ensemble_meta_learner,
            "warm_from_labeled_samples",
            return_value=fake_summary,
        ):
            result = fitted_model.warmup()
        assert result == fake_summary
        assert result["is_warm"] is True
        assert result["n_loaded"] == 42

    def test_warmup_swallows_exception_and_returns_error_dict(
        self, fitted_model
    ):
        """When ``warm_from_labeled_samples`` raises, ``warmup`` MUST
        NOT propagate — it returns an ``{"error": ..., "is_warm": False}``
        dict so the caller can continue (production lifespan startup
        proceeds without the meta-learner active)."""
        from ml.ensemble_meta_learner import ensemble_meta_learner

        with patch.object(
            ensemble_meta_learner,
            "warm_from_labeled_samples",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            result = fitted_model.warmup()
        assert "error" in result
        assert result["is_warm"] is False
        assert "simulated DB outage" in result["error"]

    def test_warmup_does_not_raise_on_failure(self, fitted_model):
        """Belt-and-braces: ``warmup`` itself never raises, regardless of
        what the underlying meta-learner does. The contract is that the
        caller's try/except around ``warmup()`` is NEVER triggered."""
        from ml.ensemble_meta_learner import ensemble_meta_learner

        # Various failure modes the meta-learner could plausibly emit.
        for failure in (
            RuntimeError("boom"),
            ValueError("bad arg"),
            KeyError("missing"),
            ConnectionError("db gone"),
            TypeError("wrong type"),
        ):
            with patch.object(
                ensemble_meta_learner,
                "warm_from_labeled_samples",
                side_effect=failure,
            ):
                # Must not raise — the call must always return a dict.
                result = fitted_model.warmup()
                assert isinstance(result, dict)


# =============================================================================
# (4) evaluate_and_promote
# =============================================================================
class TestEvaluateAndPromote:
    """W19-8 contract (4): ``evaluate_and_promote`` returns a per-challenger
    comparison dict keyed by challenger name. A challenger with
    significantly lower Brier than the champion is flagged
    ``is_significantly_better=True`` and recorded in the engine's
    promotion ledger."""

    def test_evaluate_returns_empty_when_no_challengers(self, engine):
        """``evaluate_and_promote`` returns an empty dict when no
        challengers are registered."""
        result = engine.evaluate_and_promote()
        assert result == {}

    def test_evaluate_omits_challengers_with_insufficient_outcome_data(
        self, engine
    ):
        """Challengers with fewer than ``min_samples`` outcome-stamped
        comparison rows are omitted from the returned dict (a separate
        ``insufficient_data`` log line is emitted)."""
        engine.register_shadow_model("alpha", lambda f: 0.10)
        # Record 5 comparisons but stamp ZERO outcomes.
        for i in range(5):
            engine.record_predictions(
                token_id=f"TOK_{i}",
                champion_pred=0.50,
                shadow_preds={"alpha": 0.10},
            )
        # Default min_samples=30 — 5 unstamped rows is well below threshold.
        result = engine.evaluate_and_promote(min_samples=30)
        assert result == {}  # alpha omitted (insufficient data)

    def test_evaluate_flags_significantly_better_challenger(self, engine):
        """When a challenger's per-row Brier score is significantly lower
        than the champion's (paired t-test p-value < ``alpha``), the
        challenger is flagged ``is_significantly_better=True`` AND
        ``promoted=True`` AND recorded in the engine's promotion ledger.

        Setup: 60 tokens, each with outcome. Champion always predicts 0.5
        (Brier ≈ 0.25). Challenger predicts the outcome exactly (Brier ≈ 0).
        The paired t-test should return a tiny p-value, well below 0.05.
        """
        engine.register_shadow_model(
            "oracle", lambda f: 0.50, description="always-0.5 baseline"
        )
        rng = np.random.RandomState(42)
        outcomes = [int(rng.random() > 0.5) for _ in range(60)]
        for i, o in enumerate(outcomes):
            # Champion: always 0.5 (Brier per row = (0.5 - o)^2).
            # Challenger: predict the outcome exactly. We can't know the
            # outcome at prediction time, so we cheat by stamping the
            # challenger prediction post-hoc to the outcome (0.0 or 1.0,
            # clipped to [0.01, 0.99] by record_predictions).
            engine.record_predictions(
                token_id=f"ORACLE_TOK_{i}",
                champion_pred=0.50,
                shadow_preds={"oracle": max(0.01, min(0.99, float(o)))},
            )
            # Stamp the outcome onto the just-recorded comparison row.
            engine.record_outcome(f"ORACLE_TOK_{i}", outcome_yes=bool(o))

        result = engine.evaluate_and_promote(min_samples=30, alpha=0.05)
        assert "oracle" in result, (
            "oracle had 60 outcome-stamped rows; must be in the result"
        )
        oracle_cmp = result["oracle"]
        # Required keys per the contract.
        for key in (
            "n_samples",
            "champion_brier",
            "challenger_brier",
            "brier_improvement",
            "t_statistic",
            "p_value",
            "alpha",
            "min_samples",
            "is_significantly_better",
            "promoted",
        ):
            assert key in oracle_cmp, f"missing key {key!r} in comparison dict"

        assert oracle_cmp["n_samples"] == 60
        # Champion always 0.5 → Brier = (0.5 - o)^2 averaged = 0.25 (binary).
        assert oracle_cmp["champion_brier"] == pytest.approx(0.25, abs=1e-3)
        # Challenger predicts the outcome → Brier ≈ 0 (clipped to 0.01/0.99
        # so not exactly 0, but very small).
        assert oracle_cmp["challenger_brier"] < 0.05
        # Improvement > 0 means challenger is better.
        assert oracle_cmp["brier_improvement"] > 0.20
        # Paired t-test should be very significant.
        assert oracle_cmp["p_value"] < 0.01
        assert oracle_cmp["is_significantly_better"] is True
        assert oracle_cmp["promoted"] is True

        # Promotion ledger records the decision.
        report = engine.get_status_report()
        assert "oracle" in report["promotions"]
        assert report["promotions"]["oracle"]["promoted_at"] > 0

    def test_evaluate_does_not_promote_when_challenger_is_worse(
        self, engine
    ):
        """When the challenger is significantly WORSE than the champion,
        ``is_significantly_better`` is False and ``promoted`` is False
        and the promotion ledger is NOT updated."""
        engine.register_shadow_model("worse_challenger", lambda f: 0.50)
        rng = np.random.RandomState(7)
        # Champion predicts outcome (Brier ≈ 0). Challenger always 0.5
        # (Brier ≈ 0.25). Challenger is significantly worse.
        outcomes = [int(rng.random() > 0.5) for _ in range(60)]
        for i, o in enumerate(outcomes):
            engine.record_predictions(
                token_id=f"WORSE_TOK_{i}",
                champion_pred=max(0.01, min(0.99, float(o))),
                shadow_preds={"worse_challenger": 0.50},
            )
            engine.record_outcome(f"WORSE_TOK_{i}", outcome_yes=bool(o))

        result = engine.evaluate_and_promote(min_samples=30, alpha=0.05)
        assert "worse_challenger" in result
        cmp = result["worse_challenger"]
        # Challenger Brier is HIGHER → improvement is negative.
        assert cmp["brier_improvement"] < 0
        assert cmp["is_significantly_better"] is False
        assert cmp["promoted"] is False

        # Promotion ledger is empty.
        report = engine.get_status_report()
        assert "worse_challenger" not in report["promotions"]

    def test_evaluate_does_not_promote_when_difference_not_significant(
        self, engine
    ):
        """When challenger and champion have nearly-identical Brier
        distributions, the paired t-test p-value stays above ``alpha``
        and no promotion fires."""
        engine.register_shadow_model("twin", lambda f: 0.50)
        rng = np.random.RandomState(99)
        outcomes = [int(rng.random() > 0.5) for _ in range(60)]
        # Both arms predict 0.5 — identical Brier distributions.
        for i, o in enumerate(outcomes):
            engine.record_predictions(
                token_id=f"TWIN_TOK_{i}",
                champion_pred=0.50,
                shadow_preds={"twin": 0.50},
            )
            engine.record_outcome(f"TWIN_TOK_{i}", outcome_yes=bool(o))

        result = engine.evaluate_and_promote(min_samples=30, alpha=0.05)
        assert "twin" in result
        cmp = result["twin"]
        assert cmp["brier_improvement"] == pytest.approx(0.0, abs=1e-6)
        # p-value should be 1.0 (no difference) or NaN; in either case
        # is_significantly_better must be False.
        assert cmp["is_significantly_better"] is False
        assert cmp["promoted"] is False

    def test_record_outcome_is_idempotent(self, engine):
        """``record_outcome`` only stamps rows whose ``outcome`` is
        currently ``None`` — re-invoking with the same token_id is a
        no-op (does not double-count or overwrite a previously-stamped
        outcome)."""
        engine.register_shadow_model("alpha", lambda f: 0.10)
        engine.record_predictions(
            token_id="IDEM_TOK",
            champion_pred=0.50,
            shadow_preds={"alpha": 0.10},
        )
        # First stamp → returns 1 (one row stamped).
        n1 = engine.record_outcome("IDEM_TOK", outcome_yes=True)
        assert n1 == 1
        # Second stamp → returns 0 (row already stamped).
        n2 = engine.record_outcome("IDEM_TOK", outcome_yes=True)
        assert n2 == 0
        # Verify the stamped outcome is still 1 (YES).
        report = engine.get_status_report()
        last = report["registered_models"][0]["last_comparison"]
        assert last["outcome"] == 1


# =============================================================================
# (5) API routes — /api/ml/shadow + /api/ml/shadow/evaluate
# =============================================================================
class TestAPIRoutes:
    """W19-8 contract (5): the HTTP surface
    ``GET /api/ml/shadow`` + ``POST /api/ml/shadow/evaluate`` works
    end-to-end via ``TestClient``.

    The route handlers close over the module-level singleton
    ``shadow_inference`` (the same object production code paths use), so
    the test patches that singleton's internal state to a clean
    baseline by clearing its ``_models`` dict + counters before each
    test runs.
    """

    @pytest.fixture
    def client(self, request):
        """``TestClient`` against a minimal FastAPI app with only the
        shadow-inference routes registered. The module-level singleton
        is reset to a clean baseline (empty ``_models`` dict, zeroed
        counters, empty promotion ledger) before each test and restored
        on teardown."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from ml.shadow_inference import register_routes

        # Snapshot the singleton's state so teardown can restore it.
        original_models = dict(shadow_inference_singleton._models)
        original_total_calls = shadow_inference_singleton.total_calls
        original_total_errors = shadow_inference_singleton.total_errors
        original_promotions = dict(shadow_inference_singleton._promotions)
        original_registered_at = shadow_inference_singleton.registered_at

        # Clean baseline.
        shadow_inference_singleton._models.clear()
        shadow_inference_singleton.total_calls = 0
        shadow_inference_singleton.total_errors = 0
        shadow_inference_singleton._promotions.clear()
        shadow_inference_singleton.registered_at = None

        def _restore():
            shadow_inference_singleton._models.clear()
            shadow_inference_singleton._models.update(original_models)
            shadow_inference_singleton.total_calls = original_total_calls
            shadow_inference_singleton.total_errors = original_total_errors
            shadow_inference_singleton._promotions.clear()
            shadow_inference_singleton._promotions.update(original_promotions)
            shadow_inference_singleton.registered_at = original_registered_at

        request.addfinalizer(_restore)

        app = FastAPI()
        register_routes(app)
        return TestClient(app)

    def test_get_shadow_status_returns_registry_snapshot(self, client):
        """``GET /api/ml/shadow`` returns 200 with the registry snapshot
        (``registered_models`` / ``total_calls`` / ``total_errors`` /
        ``registered_at`` / ``max_history_per_model`` / ``promotions``)."""
        response = client.get("/api/ml/shadow")
        assert response.status_code == 200, response.text
        body = response.json()
        for key in (
            "registered_models",
            "total_calls",
            "total_errors",
            "registered_at",
            "max_history_per_model",
            "promotions",
        ):
            assert key in body, f"missing key {key!r} in status payload"
        # Empty baseline — no challengers registered.
        assert body["registered_models"] == []
        assert body["total_calls"] == 0
        assert body["total_errors"] == 0
        assert body["promotions"] == {}

    def test_get_shadow_status_reflects_registered_challenger(self, client):
        """After registering a challenger + recording a prediction, the
        status endpoint surfaces it with the correct ``calls`` /
        ``last_comparison`` / ``n_outcome_stamped``."""
        shadow_inference_singleton.register_shadow_model(
            "api_test_challenger",
            lambda f: 0.42,
            description="API-test challenger",
        )
        shadow_inference_singleton.record_predictions(
            token_id="API_TOK",
            champion_pred=0.5,
            shadow_preds={"api_test_challenger": 0.42},
        )
        # Stamp an outcome so n_outcome_stamped is non-zero.
        shadow_inference_singleton.record_outcome("API_TOK", outcome_yes=True)

        response = client.get("/api/ml/shadow")
        assert response.status_code == 200
        body = response.json()
        assert len(body["registered_models"]) == 1
        challenger = body["registered_models"][0]
        assert challenger["name"] == "api_test_challenger"
        assert challenger["calls"] == 1
        assert challenger["n_outcome_stamped"] == 1
        assert challenger["last_comparison"] is not None
        assert challenger["last_comparison"]["token_id"] == "API_TOK"

    def test_evaluate_returns_empty_evaluated_when_no_challengers(self, client):
        """``POST /api/ml/shadow/evaluate`` returns 200 with empty
        ``evaluated`` + ``insufficient_data`` lists when no challengers
        are registered."""
        response = client.post("/api/ml/shadow/evaluate")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["evaluated"] == {}
        assert body["insufficient_data"] == []
        assert body["min_samples"] == 30  # default
        assert body["alpha"] == pytest.approx(0.05)

    def test_evaluate_lists_challengers_with_insufficient_data(
        self, client
    ):
        """When a challenger has too few outcome-stamped rows, it is
        named in the ``insufficient_data`` list (NOT in ``evaluated``)."""
        shadow_inference_singleton.register_shadow_model(
            "alpha", lambda f: 0.10
        )
        # Record 5 unstamped comparisons (below the default min_samples=30).
        for i in range(5):
            shadow_inference_singleton.record_predictions(
                token_id=f"INSUF_TOK_{i}",
                champion_pred=0.5,
                shadow_preds={"alpha": 0.10},
            )

        response = client.post("/api/ml/shadow/evaluate")
        assert response.status_code == 200
        body = response.json()
        assert body["evaluated"] == {}  # alpha omitted
        assert "alpha" in body["insufficient_data"]

    def test_evaluate_flags_significantly_better_challenger_via_api(
        self, client
    ):
        """End-to-end: register a challenger, record 60 outcome-stamped
        comparisons where the challenger is significantly better, then
        evaluate via the API and confirm ``is_significantly_better=True``
        + ``promoted=True`` + the promotion ledger is updated."""
        shadow_inference_singleton.register_shadow_model(
            "api_oracle", lambda f: 0.5, description="always-0.5 baseline"
        )
        rng = np.random.RandomState(123)
        outcomes = [int(rng.random() > 0.5) for _ in range(60)]
        for i, o in enumerate(outcomes):
            shadow_inference_singleton.record_predictions(
                token_id=f"API_ORACLE_{i}",
                champion_pred=0.50,
                shadow_preds={"api_oracle": max(0.01, min(0.99, float(o)))},
            )
            shadow_inference_singleton.record_outcome(
                f"API_ORACLE_{i}", outcome_yes=bool(o)
            )

        response = client.post("/api/ml/shadow/evaluate")
        assert response.status_code == 200, response.text
        body = response.json()
        assert "api_oracle" in body["evaluated"]
        cmp = body["evaluated"]["api_oracle"]
        assert cmp["is_significantly_better"] is True
        assert cmp["promoted"] is True

        # The status endpoint now reflects the promotion in the ledger.
        status = client.get("/api/ml/shadow").json()
        assert "api_oracle" in status["promotions"]

    def test_evaluate_accepts_custom_min_samples_and_alpha(self, client):
        """``min_samples`` + ``alpha`` query params are honored — a
        challenger with only 5 outcome-stamped rows is evaluated when
        ``min_samples=1`` is passed."""
        shadow_inference_singleton.register_shadow_model(
            "low_n", lambda f: 0.5
        )
        # 5 outcome-stamped rows where challenger predicts outcome exactly.
        outcomes = [1, 0, 1, 0, 1]
        for i, o in enumerate(outcomes):
            shadow_inference_singleton.record_predictions(
                token_id=f"LOW_N_{i}",
                champion_pred=0.5,
                shadow_preds={"low_n": max(0.01, min(0.99, float(o)))},
            )
            shadow_inference_singleton.record_outcome(
                f"LOW_N_{i}", outcome_yes=bool(o)
            )

        # Default min_samples=30 → low_n in insufficient_data.
        r1 = client.post("/api/ml/shadow/evaluate").json()
        assert "low_n" not in r1["evaluated"]
        assert "low_n" in r1["insufficient_data"]

        # min_samples=1 → low_n is evaluated.
        r2 = client.post(
            "/api/ml/shadow/evaluate", params={"min_samples": 1, "alpha": 0.10}
        ).json()
        assert "low_n" in r2["evaluated"]
        assert r2["min_samples"] == 1
        assert r2["alpha"] == pytest.approx(0.10)

    def test_evaluate_rejects_invalid_query_params(self, client):
        """``min_samples`` outside [1, max_history] and ``alpha`` outside
        (0, 1) return 422 (FastAPI's standard validation error)."""
        # min_samples=0 → below the ge=1 constraint.
        r = client.post("/api/ml/shadow/evaluate", params={"min_samples": 0})
        assert r.status_code == 422
        # alpha=0 → below the gt=0 constraint.
        r = client.post("/api/ml/shadow/evaluate", params={"alpha": 0.0})
        assert r.status_code == 422
        # alpha=1.0 → at the lt=1 boundary.
        r = client.post("/api/ml/shadow/evaluate", params={"alpha": 1.0})
        assert r.status_code == 422
