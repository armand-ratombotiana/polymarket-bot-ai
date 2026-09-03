"""
tests/test_shadow_inference.py — Unit tests for ``ml/shadow_inference.py``.

W7 — Shadow Inference Engine unit tests.

Covers the six public-surface guarantees enumerated in the W7 task spec:

  1. ``register_shadow_model`` ADDS a challenger to the registry — after a
     single registration the ``registered_models`` property lists exactly
     that name (and nothing else).
  2. ``register_shadow_model`` is idempotent — re-registering the SAME
     ``name`` OVERWRITES the previous entry rather than appending a
     duplicate, and the new ``fn`` / ``description`` take effect
     immediately (verified by invoking ``run_shadow`` and asserting the
     challenger's recorded p_shadow matches the SECOND fn's output).
  3. ``run_shadow`` records a prediction for EACH registered model —
     when multiple challengers are registered, a single ``run_shadow``
     call invokes every one of them once, appends a comparison entry
     to each one's history, and bumps each one's ``calls`` counter.
     The aggregate ``total_calls`` reflects the total successful
     invocations.
  4. ``run_shadow`` handles a buggy ``predict_fn`` gracefully — a
     challenger whose ``fn`` raises is caught (per-challenger
     ``total_errors`` is bumped, the engine NEVER propagates the
     exception), no comparison entry is appended for the failing
     challenger, and a SIBLING challenger that does NOT raise still
     records its comparison normally.
  5. ``run_shadow`` does NOT modify ``production_p_yes`` — the caller's
     ``p_yes`` float AND the ``features`` array are byte-for-byte
     unchanged after the call (the engine treats both as read-only
     inputs; the only writes are into the engine's own internal
     history ring buffer). The contract is verified for the float
     (caller-binding unchanged) and for a numpy ``features`` array
     (no in-place mutation).
  6. ``get_status_report`` returns the list of registered models with
     each challenger's per-challenger ``calls`` count, the most-recent
     comparison record, plus the aggregate ``total_calls`` /
     ``total_errors`` counters.

Module isolation
----------------
``ml/shadow_inference.py`` is pure-Python + synchronous — no DB, no
async I/O, no env vars. The engine's only shared state is the
module-level singleton ``shadow_inference = ShadowInferenceEngine()``
constructed at import time. To keep every test hermetic to that
singleton (the singleton persists across the whole pytest session),
each test uses the per-test ``engine`` fixture, which returns a
brand-new ``ShadowInferenceEngine()`` instance — the singleton is
left untouched. There is no module-level ``pytestmark =
pytest.mark.asyncio`` here — every test in this file is a plain
synchronous ``def`` (mirrors ``tests/test_ml_validation.py`` (U5)).

The repo's ``pytest.ini`` declares ``testpaths = tests`` and
``addopts = -q``; conftest.py (already at ``tests/conftest.py``) sets
the env-var redirects and inserts the project root on ``sys.path`` so
top-level imports (``ml.*``, ``core.*`` …) resolve regardless of the
cwd pytest was launched from. This file re-applies the ``sys.path``
insert defensively (mirrors ``tests/test_ml_validation.py`` /
``tests/test_features.py``) so the file can also be run in isolation
via ``python -m pytest tests/test_shadow_inference.py`` without
depending on conftest collection order.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── Bootstrap project root on sys.path (defensive; conftest.py also does this). ──
# Lets this file be run in isolation via
# ``python -m pytest tests/test_shadow_inference.py`` — the project root is
# always importable as top-level modules (``ml.*``) regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (sys.path must be set first)
import pytest  # noqa: E402  (sys.path must be set first)

from ml.shadow_inference import (  # noqa: E402
    ShadowInferenceEngine,
    shadow_inference as shadow_inference_singleton,
)

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here — every
# test in this file is a plain synchronous ``def``. The shadow inference
# engine is pure-Python (no I/O, no awaits) so there is nothing for the
# asyncio event loop to schedule. Skipping the asyncio marker keeps
# pytest-asyncio collection cost off this file entirely (mirrors
# ``tests/test_ml_validation.py``).


# ── Fixture: fresh engine per test ─────────────────────────────────────────
@pytest.fixture
def engine() -> ShadowInferenceEngine:
    """Return a brand-new ``ShadowInferenceEngine`` instance.

    Each test gets a clean registry (empty ``_models`` dict, zeroed
    ``total_calls`` / ``total_errors``) so the module-level singleton
    ``shadow_inference`` (also constructed at import time and shared
    across the whole pytest session) is never perturbed by these
    tests. The singleton is left untouched — production code paths
    that import ``shadow_inference`` directly (e.g. ``ml/model.py``'s
    ``predict()``) still see the singleton; this fixture is for
    unit-testing the engine in isolation.

    Mirrors the isolation strategy of ``isolated_store`` /
    ``isolated_risk_manager`` in ``tests/conftest.py`` (T15) — return a
    fresh instance, leave the global singleton alone.
    """
    return ShadowInferenceEngine()


# ── 1. register_shadow_model adds to registered models ────────────────────
def test_register_shadow_model_adds_to_registered_models(engine):
    """``register_shadow_model`` must ADD a challenger to the registry.

    After registering exactly one challenger with a callable ``fn`` and
    a non-empty ``name``, the ``registered_models`` property must list
    exactly that name (and nothing else). This is the load-bearing
    registration contract — every downstream behaviour (run_shadow
    iterating the registry, get_status_report listing models) depends
    on a registered model showing up in ``registered_models``.
    """
    engine.register_shadow_model(
        name="logistic_baseline",
        fn=lambda feats: 0.5,
        description="logistic regression baseline",
    )

    models = engine.registered_models
    # Exactly one challenger registered, with the name we supplied.
    assert len(models) == 1
    assert models == ["logistic_baseline"]


# ── 2. register_shadow_model is idempotent (same name updates) ─────────────
def test_register_shadow_model_is_idempotent_same_name_updates(engine):
    """Re-registering the SAME ``name`` must OVERWRITE the previous
    entry, NOT append a duplicate. The challenger ``fn`` and
    ``description`` are both replaced (the previous entry is discarded,
    including its history / call counter — a fresh entry takes its
    place). This idempotency contract is what makes the lifespan
    startup block in ``api/server.py`` (T13) safe to re-run on every
    process restart without leaking duplicate challenger entries into
    the registry.

    Verification strategy: register ``challenger_a`` twice with
    different fn + description, then invoke ``run_shadow`` and assert
    the recorded ``p_shadow`` matches the SECOND fn's output (proving
    the overwrite took effect at the call-site, not just in the
    registry listing).
    """
    def first_fn(feats):
        return 0.1

    def second_fn(feats):
        return 0.9

    # Register once
    engine.register_shadow_model(
        name="challenger_a",
        fn=first_fn,
        description="first version",
    )
    assert engine.registered_models == ["challenger_a"]

    # Register the SAME name again with different fn + description
    engine.register_shadow_model(
        name="challenger_a",
        fn=second_fn,
        description="second version",
    )

    # Idempotency: still exactly one entry for "challenger_a" — NOT two.
    models = engine.registered_models
    assert len(models) == 1
    assert models == ["challenger_a"]

    # The new fn + description took effect: invoking run_shadow with
    # the updated challenger yields the SECOND fn's p_yes value, and
    # the status report carries the new description (NOT the first).
    feats = np.zeros(5, dtype=float)
    engine.run_shadow(feats, token_id="tok_idem", p_yes=0.5)

    report = engine.get_status_report()
    assert len(report["registered_models"]) == 1
    entry = report["registered_models"][0]
    assert entry["name"] == "challenger_a"
    # Description was overwritten (second version, not first version).
    assert entry["description"] == "second version"
    # The challenger was invoked exactly once (calls counter did NOT
    # carry over from a phantom pre-existing entry — proving the
    # overwrite discarded the previous entry's state).
    assert entry["calls"] == 1
    # second_fn returns 0.9, clipped to [0.01, 0.99] → 0.9 unchanged.
    assert entry["last_comparison"]["p_shadow"] == pytest.approx(0.9)


# ── 3. run_shadow records prediction for each registered model ─────────────
def test_run_shadow_records_prediction_for_each_registered_model(engine):
    """When multiple challengers are registered, ``run_shadow`` must
    invoke EACH one exactly once per call and append a comparison
    record to EACH one's history ring buffer. The per-challenger
    ``calls`` counter and the aggregate ``total_calls`` are both
    incremented to reflect the invocations. ``total_errors`` stays at
    zero when no challenger raises.

    This is the core shadow-inference contract: every registered
    challenger sees every production prediction, with the same
    ``features`` + ``token_id`` + ``p_yes`` inputs the production
    model used, so offline A/B comparisons are apples-to-apples.
    """
    def fn_alpha(feats):
        return 0.7

    def fn_beta(feats):
        return 0.3

    engine.register_shadow_model(
        "alpha", fn_alpha, description="alpha model",
    )
    engine.register_shadow_model(
        "beta", fn_beta, description="beta model",
    )

    feats = np.array([0.1, 0.2, 0.3])
    engine.run_shadow(feats, token_id="tok_multi", p_yes=0.5)

    # Each challenger now has exactly one comparison record.
    report = engine.get_status_report()
    assert len(report["registered_models"]) == 2

    by_name = {r["name"]: r for r in report["registered_models"]}
    assert set(by_name.keys()) == {"alpha", "beta"}

    # alpha: fn returned 0.7 (within [0.01, 0.99] → unchanged after clip)
    alpha = by_name["alpha"]
    assert alpha["calls"] == 1
    assert alpha["last_comparison"] is not None
    assert alpha["last_comparison"]["token_id"] == "tok_multi"
    # Production p_yes is recorded alongside the challenger's p_shadow
    # so the comparison is self-contained.
    assert alpha["last_comparison"]["p_production"] == pytest.approx(0.5)
    assert alpha["last_comparison"]["p_shadow"] == pytest.approx(0.7)
    # abs_delta = |0.7 - 0.5| = 0.2
    assert alpha["last_comparison"]["abs_delta"] == pytest.approx(0.2)

    # beta: fn returned 0.3
    beta = by_name["beta"]
    assert beta["calls"] == 1
    assert beta["last_comparison"] is not None
    assert beta["last_comparison"]["p_shadow"] == pytest.approx(0.3)
    # abs_delta = |0.3 - 0.5| = 0.2
    assert beta["last_comparison"]["abs_delta"] == pytest.approx(0.2)

    # Aggregate total_calls == number of successful challenger invocations
    # (2 — one per registered challenger). total_errors stays at zero.
    assert report["total_calls"] == 2
    assert report["total_errors"] == 0

    # Sanity: a second run_shadow invocation doubles every counter —
    # the engine does NOT reset state between invocations.
    engine.run_shadow(feats, token_id="tok_multi_2", p_yes=0.5)
    report2 = engine.get_status_report()
    by_name2 = {r["name"]: r for r in report2["registered_models"]}
    assert by_name2["alpha"]["calls"] == 2
    assert by_name2["beta"]["calls"] == 2
    assert report2["total_calls"] == 4
    assert report2["total_errors"] == 0
    # The last_comparison for alpha now reflects the SECOND invocation's
    # token_id (the history ring buffer's most recent entry).
    assert by_name2["alpha"]["last_comparison"]["token_id"] == "tok_multi_2"


# ── 4. run_shadow handles buggy predict_fn gracefully ─────────────────────
def test_run_shadow_handles_buggy_predict_fn_gracefully(engine):
    """A challenger whose ``fn`` raises must NOT crash ``run_shadow`` —
    the engine catches the exception (broad ``except Exception``), bumps
    the aggregate ``total_errors`` counter, and continues to the next
    challenger. The failing challenger's ``calls`` counter stays at zero
    (no comparison record appended to its history); a SIBLING challenger
    that does NOT raise still records its comparison normally.

    This is the load-bearing resilience contract: the shadow-inference
    engine is invoked from inside the production ``predict()`` path
    (ml/model.py, T13 wiring), so a buggy / slow / raising challenger
    must NEVER propagate an exception back into the production
    prediction pipeline. The production model's p_yes output must be
    unaffected by a challenger crash.

    NOTE on dispatch model: every ``run_shadow`` call invokes EVERY
    registered challenger (there is no per-challenger routing). So a
    single ``run_shadow`` call with ``{buggy, good, value_err}``
    registered produces TWO errors (buggy + value_err both raise) and
    ONE successful comparison (good). The test asserts exactly that.
    """
    def buggy_fn(feats):
        raise RuntimeError("intentional test failure")

    def good_fn(feats):
        return 0.55

    def value_err_fn(feats):
        raise ValueError("intentional value error")

    engine.register_shadow_model("buggy", buggy_fn, description="raises RTE")
    engine.register_shadow_model("good", good_fn, description="works")
    engine.register_shadow_model(
        "value_err", value_err_fn, description="raises VE",
    )

    feats = np.array([0.0])
    # Must NOT raise — every challenger exception is swallowed inside
    # run_shadow's per-challenger try/except. Two challengers raise
    # (RuntimeError + ValueError); the broad ``except Exception``
    # clause covers both subclasses.
    engine.run_shadow(feats, token_id="tok_buggy", p_yes=0.5)

    report = engine.get_status_report()
    by_name = {r["name"]: r for r in report["registered_models"]}

    # Buggy challenger (RuntimeError): error swallowed, NO comparison.
    buggy = by_name["buggy"]
    assert buggy["calls"] == 0
    assert buggy["last_comparison"] is None

    # Value-error challenger (ValueError): ALSO swallowed — proves the
    # broad ``except Exception`` clause covers non-RuntimeError
    # subclasses, not just RuntimeError.
    value_err = by_name["value_err"]
    assert value_err["calls"] == 0
    assert value_err["last_comparison"] is None

    # Good challenger: comparison recorded normally — the siblings'
    # crashes did NOT abort the per-call loop.
    good = by_name["good"]
    assert good["calls"] == 1
    assert good["last_comparison"] is not None
    assert good["last_comparison"]["p_shadow"] == pytest.approx(0.55)
    assert good["last_comparison"]["token_id"] == "tok_buggy"

    # Aggregate counters reflect the partial failure:
    #   total_calls == 1  (only the good challenger succeeded)
    #   total_errors == 2  (buggy + value_err both raised)
    assert report["total_calls"] == 1
    assert report["total_errors"] == 2

    # Belt-and-braces: invoke run_shadow a SECOND time. Both raising
    # challengers raise again — total_errors climbs to 4; the good
    # challenger records a second comparison (calls=2). Proves the
    # engine does NOT cache failures or short-circuit subsequent calls.
    engine.run_shadow(feats, token_id="tok_buggy_2", p_yes=0.5)
    report2 = engine.get_status_report()
    by_name2 = {r["name"]: r for r in report2["registered_models"]}
    assert by_name2["buggy"]["calls"] == 0
    assert by_name2["value_err"]["calls"] == 0
    assert by_name2["good"]["calls"] == 2
    assert report2["total_calls"] == 2
    assert report2["total_errors"] == 4


# ── 5. run_shadow does not modify production_p_yes ─────────────────────────
def test_run_shadow_does_not_modify_production_p_yes(engine):
    """``run_shadow`` is contractually side-effect-free with respect to
    the caller's variables: the production ``p_yes`` (a float) AND the
    ``features`` array must be UNCHANGED after the call. The engine
    only READS these values to compute the comparison record — it must
    never mutate them in place (no in-place clipping of p_yes, no
    append / extend / reshape on the features array).

    The float case is trivially satisfied by Python's immutable-float
    semantics, but the test pins it down explicitly so a future
    refactor that swaps the signature to a mutable container (e.g. a
    numpy scalar or a ``list``) cannot silently regress the contract.
    The features-array case is the load-bearing one — numpy arrays are
    mutable, and the engine passing them to challenger ``fn``s means a
    buggy challenger COULD mutate them in place if the engine didn't
    guard against it. The engine itself never mutates features, and
    the test verifies that for a typical challenger fn.
    """
    def simple_fn(feats):
        # Read-only use of feats (the typical pattern): the challenger
        # computes a p_yes estimate from the feature vector WITHOUT
        # mutating it. The engine must do the same.
        return float(np.mean(feats))

    engine.register_shadow_model("simple", simple_fn)

    p_yes = 0.5
    p_yes_before = p_yes

    feats = np.array([0.1, 0.2, 0.3, 0.4])
    feats_before = feats.copy()

    engine.run_shadow(feats, token_id="tok_immut", p_yes=p_yes)

    # (a) The caller's ``p_yes`` float is unchanged — the engine does
    #     not re-bind the caller's variable (Python floats are
    #     immutable; this also guards against any future refactor that
    #     swaps to an in-out parameter style).
    assert p_yes == p_yes_before
    assert p_yes == 0.5

    # (b) The caller's ``features`` array is byte-for-byte unchanged —
    #     no in-place mutation by the engine or by the challenger fn.
    np.testing.assert_array_equal(feats, feats_before)
    # Same dtype + shape — guards against a future "smart" reshape.
    assert feats.dtype == feats_before.dtype
    assert feats.shape == feats_before.shape

    # (c) The recorded comparison's ``p_production`` reflects the value
    #     passed in (rounded to 4dp) — proving the engine READ p_yes
    #     but did NOT mutate the caller's binding. mean([0.1..0.4])=0.25
    #     so p_shadow should be 0.25 (within clip range).
    report = engine.get_status_report()
    last = report["registered_models"][0]["last_comparison"]
    assert last["p_production"] == pytest.approx(0.5)
    assert last["p_shadow"] == pytest.approx(0.25)

    # (d) Belt-and-braces: passing an EDGE-VALUE p_yes at the clip
    #     boundaries (0.01 / 0.99) does NOT mutate the caller's value
    #     either. The engine's internal clip applies to the
    #     CHALLENGER's output, never to the production p_yes.
    p_yes_edge = 0.99
    engine.run_shadow(feats, token_id="tok_edge", p_yes=p_yes_edge)
    assert p_yes_edge == 0.99   # unchanged

    p_yes_floor = 0.01
    engine.run_shadow(feats, token_id="tok_floor", p_yes=p_yes_floor)
    assert p_yes_floor == 0.01  # unchanged

    # (e) Belt-and-braces: passing a Python int (not a float) for
    #     p_yes does NOT mutate it either — the engine's ``float(p_yes)``
    #     coercion is read-only. After the call, the int binding is
    #     still the SAME int.
    p_yes_int = 1
    engine.run_shadow(feats, token_id="tok_int", p_yes=p_yes_int)
    assert p_yes_int == 1
    assert isinstance(p_yes_int, int)   # still an int, not coerced


# ── 6. get_status_report returns registered models with prediction counts ──
def test_get_status_report_returns_registered_models_with_prediction_counts(
    engine,
):
    """``get_status_report`` must return a payload whose
    ``registered_models`` list carries one entry per registered
    challenger, each with a ``calls`` counter reflecting the number of
    successful ``run_shadow`` invocations for THAT challenger and a
    ``last_comparison`` record (or ``None`` if the challenger has never
    been invoked). The report's top-level ``total_calls`` /
    ``total_errors`` aggregate across all challengers.

    This is the observability surface a future ``/api/shadow-inference``
    endpoint (T13 follow-up) would expose — the test pins down the
    payload shape so a future refactor can't silently drop keys.

    NOTE on dispatch model: every ``run_shadow`` call invokes EVERY
    registered challenger (there is no per-challenger routing). So the
    per-challenger ``calls`` counter is exactly "number of
    ``run_shadow`` invocations that happened while this challenger was
    registered". A challenger registered AFTER some invocations have
    already happened surfaces in the report with ``calls=0`` and
    ``last_comparison=None`` — this is the load-bearing contract the
    test exercises.
    """
    # Register three challengers up front: alpha, beta, gamma. After
    # three ``run_shadow`` invocations, each will have calls=3.
    engine.register_shadow_model("alpha", lambda f: 0.1, description="a")
    engine.register_shadow_model("beta", lambda f: 0.2, description="b")
    engine.register_shadow_model("gamma", lambda f: 0.3, description="c")

    feats = np.array([0.5])
    # Three run_shadow invocations — every registered challenger is
    # invoked on each call, so alpha/beta/gamma each accumulate calls=3.
    engine.run_shadow(feats, token_id="tok_1", p_yes=0.5)
    engine.run_shadow(feats, token_id="tok_2", p_yes=0.5)
    engine.run_shadow(feats, token_id="tok_3", p_yes=0.5)

    report = engine.get_status_report()

    # (a) Top-level shape: must carry the registered_models list + the
    #     aggregate counters. Pins down the public payload contract.
    assert "registered_models" in report
    assert "total_calls" in report
    assert "total_errors" in report
    assert "registered_at" in report
    assert "max_history_per_model" in report

    # (b) Exactly three challenger entries — one per registered model.
    assert len(report["registered_models"]) == 3
    by_name = {r["name"]: r for r in report["registered_models"]}
    assert set(by_name.keys()) == {"alpha", "beta", "gamma"}

    # (c) Per-challenger call counts reflect the exact invocation pattern:
    #     three run_shadow calls × three challengers → each challenger
    #     invoked three times.
    assert by_name["alpha"]["calls"] == 3
    assert by_name["beta"]["calls"] == 3
    assert by_name["gamma"]["calls"] == 3

    # (d) Per-challenger description is surfaced (the human-readable
    #     label registered alongside the fn).
    assert by_name["alpha"]["description"] == "a"
    assert by_name["beta"]["description"] == "b"
    assert by_name["gamma"]["description"] == "c"

    # (e) Each challenger has been invoked → last_comparison reflects
    #     the most-recent (third) invocation's token_id ("tok_3") —
    #     the history ring buffer's tail is what the report surfaces.
    for name in ("alpha", "beta", "gamma"):
        last = by_name[name]["last_comparison"]
        assert last is not None
        assert last["token_id"] == "tok_3"
        assert last["p_production"] == pytest.approx(0.5)
    # The challenger-specific p_shadow values are surfaced too.
    assert by_name["alpha"]["last_comparison"]["p_shadow"] == pytest.approx(0.1)
    assert by_name["beta"]["last_comparison"]["p_shadow"] == pytest.approx(0.2)
    assert by_name["gamma"]["last_comparison"]["p_shadow"] == pytest.approx(0.3)

    # (f) Per-challenger mean_abs_delta_vs_production: with p_yes=0.5,
    #     |0.1 - 0.5| = 0.4 (alpha), |0.2 - 0.5| = 0.3 (beta),
    #     |0.3 - 0.5| = 0.2 (gamma). Each challenger was invoked 3×
    #     with the same p_yes and the same fn output, so the rolling
    #     mean is the same as the single-invocation delta.
    assert by_name["alpha"]["mean_abs_delta_vs_production"] == pytest.approx(0.4)
    assert by_name["beta"]["mean_abs_delta_vs_production"] == pytest.approx(0.3)
    assert by_name["gamma"]["mean_abs_delta_vs_production"] == pytest.approx(0.2)

    # (g) Aggregate total_calls == sum of per-challenger calls.
    #     3 challengers × 3 invocations each = 9 successful calls.
    assert report["total_calls"] == 9
    assert report["total_errors"] == 0

    # (h) Belt-and-braces: registering an additional challenger AFTER
    #     the run_shadow calls have already happened surfaces it in the
    #     report with calls=0 and last_comparison=None — the report is
    #     a LIVE snapshot, not a cached copy. A challenger that has
    #     never been invoked must report last_comparison=None (NOT an
    #     empty dict, NOT a placeholder record).
    engine.register_shadow_model("delta", lambda f: 0.4, description="d")
    report2 = engine.get_status_report()
    assert len(report2["registered_models"]) == 4
    by_name2 = {r["name"]: r for r in report2["registered_models"]}
    assert by_name2["delta"]["calls"] == 0
    assert by_name2["delta"]["last_comparison"] is None
    assert by_name2["delta"]["mean_abs_delta_vs_production"] == pytest.approx(0.0)
    # Previously-registered challengers' counts are UNCHANGED by the
    # new registration (delta was not invoked retroactively).
    assert by_name2["alpha"]["calls"] == 3
    assert by_name2["beta"]["calls"] == 3
    assert by_name2["gamma"]["calls"] == 3
    assert report2["total_calls"] == 9   # unchanged
