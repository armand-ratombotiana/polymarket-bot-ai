"""W35-2 — Unit tests for ``ingestion/adaptive_poller.py``.

Covers the seven behavioural surfaces enumerated in the W35-2 task
spec:

  1. **Adaptive interval adjustment** — a token's polling interval
     changes (ACTIVE → NORMAL → INACTIVE) as its trade activity
     decays. (Section A.)
  2. **Rate-limit backoff** — a 429 response forces the tier to
     ``RATE_LIMITED`` (60 s); gradual recovery steps the tier down
     on each successful poll. (Section B.)
  3. **Market activity detection** — ``record_trade`` promotes a
     token toward ACTIVE tier; ``trades_in_window`` returns the
     expected count. (Section C.)
  4. **Error recovery** — non-429 errors trigger exponential
     backoff (1 s → 2 s → 4 s); first success zeroes the streak.
     (Section D.)
  5. **Market resolution** — ``mark_resolved`` removes the token
     from the polling set + fires the ``on_resolution`` hook.
     (Section E.)
  6. **New-market immediate polling** — ``add_token`` registers
     a token with ``last_polled_at=0`` so the next ``tick`` polls
     it immediately. (Section F.)
  7. **Rate-limit-aware polling** — ``update_rate_limit`` parses
     ``X-RateLimit-*`` headers; ``next_interval_for`` slows down
     when the budget drops below the caution / critical thresholds.
     (Section G.)

Isolation
~~~~~~~~~~

  * Every test constructs a FRESH ``AdaptivePoller`` instance per
    test (NOT the module-level singleton) so the per-token state +
    the global rate-limit state can't leak between tests.

  * The fetcher callable is injected per-test as a closure over a
    ``dict[token_id, FetchResult]`` so the test can drive
    deterministic success / 429 / error sequences without spinning
    up a real ``httpx.AsyncClient``. Mirrors the ``mock_gamma`` /
    ``mock_timescale`` pattern in ``tests/test_book_poller.py``.

  * Time is controlled via ``time.time`` monkeypatch (or explicit
    ``ts=`` arguments to ``record_trade``) so the tier classification
    is deterministic regardless of wall-clock drift between the test
    process and the CI runner.

  * ``asyncio.sleep`` is NOT patched — the tests don't run the
    background ``_loop`` (they call ``tick()`` directly so the
    scheduler's sleep is bypassed). This keeps the tests fast (no
    1 s wait between ticks) and deterministic (no race between the
    test's assertion and the loop's next iteration).

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration — mirrors every
sibling test module (``tests/test_book_poller.py``,
``tests/test_ws_ingestion.py`` etc.) since the repo's ``pytest.ini``
cannot be edited per the additive-files constraint.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

# ── sys.path hygiene ────────────────────────────────────────────────────────
# The W31-7 sibling test suite at ``tests/ingestion/`` ships a
# ``tests/ingestion/__init__.py`` — pytest's test-package discovery
# inserts ``tests/`` onto ``sys.path`` (ahead of the project root the
# shared ``tests/conftest.py`` inserts), which makes ``import
# ingestion`` resolve to the W31-7 test subpackage rather than the
# top-level ``ingestion/`` package. To resolve the ambiguity, pop the
# ``tests/`` directory from ``sys.path`` BEFORE the
# ``from ingestion.adaptive_poller import ...`` line so Python finds
# the top-level ``ingestion/`` package first. The project root is
# already on ``sys.path`` (inserted by ``tests/conftest.py``), so the
# top-level package is reachable. Mirrors the W31-2
# ``tests/test_ws_ingestion.py`` sys.path hygiene block.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = str(Path(__file__).resolve().parent)
sys.path = [p for p in sys.path if p != _TESTS_DIR]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ingestion.adaptive_poller import (  # noqa: E402
    ACTIVE_INTERVAL,
    ACTIVE_TRADE_RECENCY_S,
    ACTIVE_TRADE_VOLUME_THRESHOLD,
    BACKOFF_MULTIPLIER,
    BASE_BACKOFF_S,
    INACTIVE_INTERVAL,
    MAX_BACKOFF_S,
    NORMAL_INTERVAL,
    RATE_LIMIT_CAUTION_THRESHOLD,
    RATE_LIMIT_CRITICAL_THRESHOLD,
    RATE_LIMITED_INTERVAL,
    AdaptivePoller,
    FetchResult,
    MarketActivity,
    PollingTier,
    RateLimitState,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_book_poller.py``:
# the repo's ``pytest.ini`` cannot be edited per the additive-files
# constraint, so we use the module-level ``pytestmark`` idiom instead
# of ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _ok_result(
    token_id: str, *, last_trade_ts: float | None = None
) -> FetchResult:
    """Build a successful ``FetchResult`` for ``token_id``.

    ``last_trade_ts`` lets the test simulate a fetch whose payload
    contained a recent trade — the poller's ``_apply_result`` will
    ``record_trade`` it so the tier classification promotes the token
    without an out-of-band ``record_trade`` call.
    """
    return FetchResult(
        token_id=token_id,
        ok=True,
        error="",
        is_rate_limited=False,
        headers=None,
        last_trade_ts=last_trade_ts,
        payload={"bids": [], "asks": []},
    )


def _rate_limited_result(token_id: str) -> FetchResult:
    """Build a 429-rate-limited ``FetchResult`` for ``token_id``."""
    return FetchResult(
        token_id=token_id,
        ok=False,
        error="429 Too Many Requests",
        is_rate_limited=True,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "100"},
        payload=None,
    )


def _error_result(token_id: str, msg: str = "simulated failure") -> FetchResult:
    """Build a non-429 error ``FetchResult`` for ``token_id``."""
    return FetchResult(
        token_id=token_id,
        ok=False,
        error=msg,
        is_rate_limited=False,
        headers=None,
        payload=None,
    )


def _make_fetcher(
    responses: dict[str, FetchResult] | list[FetchResult],
) -> "Any":
    """Build a deterministic fetcher callable.

    Two modes:

      * ``dict[token_id, FetchResult]`` — each token always gets the
        same response (used by single-shot tests).
      * ``list[FetchResult]`` — the fetcher pops the next response
        off the list per call (used by sequential tests that drive
        a token through success → 429 → success → success).

    The returned callable is an ``async def`` so the poller's
    ``await self._fetcher(tid)`` call resolves cleanly.
    """
    if isinstance(responses, dict):
        async def _fetch_dict(tid: str) -> FetchResult:
            return responses.get(tid, _ok_result(tid))

        return _fetch_dict

    queue = list(responses)

    async def _fetch_list(tid: str) -> FetchResult:
        if not queue:
            return _ok_result(tid)
        result = queue.pop(0)
        # Override the token_id so the fetcher is token-agnostic
        # (the test sets up a list of responses for the SAME token).
        return FetchResult(
            token_id=tid,
            ok=result.ok,
            error=result.error,
            is_rate_limited=result.is_rate_limited,
            headers=result.headers,
            last_trade_ts=result.last_trade_ts,
            payload=result.payload,
        )

    return _fetch_list


# ────────────────────────────────────────────────────────────────────────────
# Section A — Adaptive interval adjustment
# ────────────────────────────────────────────────────────────────────────────


async def test_tier_inactive_for_token_with_no_trades():
    """A token with zero recorded trades is classified ``INACTIVE``.

    The base interval for ``INACTIVE`` is ``INACTIVE_INTERVAL`` (30 s).
    """
    poller = AdaptivePoller()
    poller.add_token("T1")

    tier = poller.classify("T1")
    assert tier == PollingTier.INACTIVE, (
        f"expected INACTIVE for token with no trades; got {tier}"
    )
    interval = poller.next_interval_for("T1")
    assert interval == INACTIVE_INTERVAL, (
        f"expected {INACTIVE_INTERVAL}s; got {interval}s"
    )


async def test_tier_active_for_token_with_recent_trade_burst():
    """A token with ≥ ``ACTIVE_TRADE_VOLUME_THRESHOLD`` trades in the
    last ``ACTIVE_TRADE_RECENCY_S`` window is classified ``ACTIVE``.

    The base interval for ``ACTIVE`` is ``ACTIVE_INTERVAL`` (1 s).
    """
    poller = AdaptivePoller()
    poller.add_token("T1")

    # Record ACTIVE_TRADE_VOLUME_THRESHOLD trades at "now" so they
    # all fall within the ACTIVE recency window.
    now = time.time()
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=now)

    tier = poller.classify("T1")
    assert tier == PollingTier.ACTIVE, (
        f"expected ACTIVE after {ACTIVE_TRADE_VOLUME_THRESHOLD} trades; "
        f"got {tier}"
    )
    interval = poller.next_interval_for("T1")
    assert interval == ACTIVE_INTERVAL, (
        f"expected {ACTIVE_INTERVAL}s; got {interval}s"
    )


async def test_tier_normal_for_token_with_one_recent_trade():
    """A token with ONE trade in the last ``NORMAL_TRADE_RECENCY_S``
    window (but fewer than the ACTIVE volume threshold) is
    classified ``NORMAL``.

    The base interval for ``NORMAL`` is ``NORMAL_INTERVAL`` (5 s).
    """
    poller = AdaptivePoller()
    poller.add_token("T1")

    # Record a single trade at "now" — below the ACTIVE volume
    # threshold but within the NORMAL recency window.
    poller.record_trade("T1", ts=time.time())

    tier = poller.classify("T1")
    assert tier == PollingTier.NORMAL, (
        f"expected NORMAL after 1 trade; got {tier}"
    )
    interval = poller.next_interval_for("T1")
    assert interval == NORMAL_INTERVAL, (
        f"expected {NORMAL_INTERVAL}s; got {interval}s"
    )


async def test_tier_demotes_active_to_inactive_as_trades_age_out():
    """A token's tier must decay ACTIVE → NORMAL → INACTIVE as its
    recent trades age out of the recency windows.

    Sequence:
      * t=0      : 5 trades recorded → ACTIVE.
      * t=120    : trades still in NORMAL window (300 s) but outside
                   ACTIVE window (60 s) → NORMAL.
      * t=400    : trades outside both windows → INACTIVE.

    Belt-and-braces: the tier transitions are observed in order, and
    the ``next_interval_for`` matches the tier's base interval at
    each step (no rate-limit slow-down active during this test).
    """
    poller = AdaptivePoller()
    poller.add_token("T1")

    # Record 5 trades at t=0 (ACTIVE burst).
    base_t = 1_000_000.0  # synthetic anchor so test is hermetic to wall clock
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=base_t)

    # ACTIVE at t=base_t.
    assert poller.classify("T1", now=base_t) == PollingTier.ACTIVE
    assert poller.next_interval_for("T1", now=base_t) == ACTIVE_INTERVAL

    # NORMAL at t=base_t + 120 (outside 60 s ACTIVE window, inside
    # 300 s NORMAL window).
    t_normal = base_t + 120.0
    assert poller.classify("T1", now=t_normal) == PollingTier.NORMAL, (
        "expected NORMAL when trades are 120 s old (outside ACTIVE window, "
        "inside NORMAL window)"
    )
    assert poller.next_interval_for("T1", now=t_normal) == NORMAL_INTERVAL

    # INACTIVE at t=base_t + 400 (outside both windows).
    t_inactive = base_t + 400.0
    assert poller.classify("T1", now=t_inactive) == PollingTier.INACTIVE, (
        "expected INACTIVE when trades are 400 s old (outside both windows)"
    )
    assert poller.next_interval_for("T1", now=t_inactive) == INACTIVE_INTERVAL


async def test_tier_intervals_respect_custom_constructor_overrides():
    """Custom interval overrides passed to ``__init__`` propagate to
    ``next_interval_for`` for every tier."""
    poller = AdaptivePoller(
        active_interval=0.5,
        normal_interval=10.0,
        inactive_interval=60.0,
        rate_limited_interval=120.0,
    )
    poller.add_token("T1")
    assert poller.next_interval_for("T1") == 60.0  # inactive default

    poller.record_trade("T1", ts=time.time())
    assert poller.next_interval_for("T1") == 10.0  # normal

    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=time.time())
    assert poller.next_interval_for("T1") == 0.5  # active


# ────────────────────────────────────────────────────────────────────────────
# Section B — Rate-limit backoff (429 → 60 s, gradual recovery)
# ────────────────────────────────────────────────────────────────────────────


async def test_rate_limited_tier_after_429_response():
    """A 429 response forces the token's tier to ``RATE_LIMITED`` and
    the interval to ``RATE_LIMITED_INTERVAL`` (60 s).

    The override takes precedence over the activity-based tier —
    a token with 5 recent trades would otherwise be ACTIVE (1 s),
    but after a 429 it MUST cool down.
    """
    poller = AdaptivePoller(fetcher=_make_fetcher({"T1": _rate_limited_result("T1")}))
    poller.add_token("T1")
    # Pre-promote to ACTIVE so we can prove the 429 overrides it.
    now = time.time()
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.ACTIVE  # precondition

    # Run a single tick — fetcher returns 429.
    await poller.tick()

    market = poller._markets["T1"]  # noqa: SLF001 — direct state inspection
    assert market.consecutive_429s == 1, (
        f"expected consecutive_429s=1 after one 429; got {market.consecutive_429s}"
    )
    tier = poller.classify("T1")
    assert tier == PollingTier.RATE_LIMITED, (
        f"expected RATE_LIMITED tier after 429; got {tier}"
    )
    interval = poller.next_interval_for("T1")
    assert interval == RATE_LIMITED_INTERVAL, (
        f"expected {RATE_LIMITED_INTERVAL}s after 429; got {interval}s"
    )
    # The global rate-limit event counter increments.
    assert poller.stats()["rate_limit_events"] == 1


async def test_rate_limit_recovery_decrements_streak_on_success():
    """Each successful poll after a 429 decrements the streak by one
    so the tier steps down RATE_LIMITED → RATE_LIMITED → ...
    → activity tier (gradual recovery).

    Sequence (3 consecutive 429s, then 3 successes):
      * 1st 429 → consecutive_429s=1 → RATE_LIMITED.
      * 2nd 429 → consecutive_429s=2 → RATE_LIMITED.
      * 3rd 429 → consecutive_429s=3 → RATE_LIMITED.
      * 1st success → consecutive_429s=2 → RATE_LIMITED.
      * 2nd success → consecutive_429s=1 → RATE_LIMITED.
      * 3rd success → consecutive_429s=0 → activity tier.
    """
    # Three 429s followed by three successes for the same token.
    responses = [
        _rate_limited_result("T1"),
        _rate_limited_result("T1"),
        _rate_limited_result("T1"),
        _ok_result("T1"),
        _ok_result("T1"),
        _ok_result("T1"),
    ]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    # Drive three 429s. Use force=True so each tick re-polls the
    # token regardless of the RATE_LIMITED cadence (the per-token
    # last_polled_at would otherwise gate the second tick behind
    # the 60 s RATE_LIMITED_INTERVAL).
    await poller.tick(force=True)  # 1st 429
    await poller.tick(force=True)  # 2nd 429
    await poller.tick(force=True)  # 3rd 429

    market = poller._markets["T1"]  # noqa: SLF001
    assert market.consecutive_429s == 3, (
        f"expected streak=3 after three 429s; got {market.consecutive_429s}"
    )
    assert poller.classify("T1") == PollingTier.RATE_LIMITED

    # First success: streak decays from 3 → 2.
    await poller.tick(force=True)
    assert market.consecutive_429s == 2, (
        f"expected streak=2 after one success; got {market.consecutive_429s}"
    )
    assert poller.classify("T1") == PollingTier.RATE_LIMITED

    # Second success: streak decays from 2 → 1.
    await poller.tick(force=True)
    assert market.consecutive_429s == 1, (
        f"expected streak=1 after two successes; got {market.consecutive_429s}"
    )
    assert poller.classify("T1") == PollingTier.RATE_LIMITED

    # Third success: streak decays from 1 → 0 → tier drops back to
    # activity-based (INACTIVE — no trades recorded).
    await poller.tick(force=True)
    assert market.consecutive_429s == 0
    assert poller.classify("T1") == PollingTier.INACTIVE


async def test_rate_limit_backoff_does_not_affect_other_tokens():
    """A 429 on one token must NOT promote other tokens to the
    ``RATE_LIMITED`` tier — the streak is per-token, not global.

    Belt-and-braces that the shared ``RateLimitState`` IS updated
    (the X-RateLimit-* headers from the 429 response populate the
    shared budget) — that's the global signal. But the
    ``consecutive_429s`` streak that gates the RATE_LIMITED tier is
    per-token, so a token that's never been 429'd stays on its
    activity-based tier.
    """
    poller = AdaptivePoller(
        fetcher=_make_fetcher({
            "T1": _rate_limited_result("T1"),
            "T2": _ok_result("T2"),
        })
    )
    poller.add_token("T1")
    poller.add_token("T2")

    await poller.tick()

    # T1 is rate-limited (got a 429).
    assert poller.classify("T1") == PollingTier.RATE_LIMITED
    # T2 is NOT rate-limited — its tier is INACTIVE (no trades).
    assert poller.classify("T2") == PollingTier.INACTIVE
    # But the shared rate-limit state WAS updated by the 429's headers.
    assert poller.rate_limit_state.remaining == 0
    assert poller.rate_limit_state.limit == 100


# ────────────────────────────────────────────────────────────────────────────
# Section C — Market activity detection
# ────────────────────────────────────────────────────────────────────────────


async def test_market_activity_trades_in_window_counts_only_recent():
    """``MarketActivity.trades_in_window(window_s)`` returns the count
    of trades whose timestamp is within ``window_s`` of ``now``.

    Records two bursts of trades (older + newer) and verifies the
    window catches only the trades whose timestamp falls within
    ``[now - window_s, now]``.
    """
    market = MarketActivity(token_id="T1")
    base_t = 1_000_000.0

    # 5 trades at base_t (older), 5 trades at base_t + 200 s (newer).
    for _ in range(5):
        market.record_trade(ts=base_t)
    for _ in range(5):
        market.record_trade(ts=base_t + 200.0)

    # At t=base_t + 250, the 60 s window covers [base_t+190, base_t+250]
    # → catches only the second burst (5 trades at base_t+200).
    assert market.trades_in_window(60.0, now=base_t + 250.0) == 5
    # At t=base_t + 250, the 300 s window covers [base_t-50, base_t+250]
    # → catches both bursts (10 trades).
    assert market.trades_in_window(300.0, now=base_t + 250.0) == 10
    # At t=base_t + 1000, both bursts are outside any reasonable
    # window → 0 trades caught.
    assert market.trades_in_window(60.0, now=base_t + 1000.0) == 0


async def test_market_activity_promotes_token_via_fetch_last_trade_ts():
    """A ``FetchResult.last_trade_ts`` value triggers
    ``MarketActivity.record_trade`` so the tier classification
    promotes the token without an explicit ``record_trade`` call.

    This is the production path — the fetcher adapter (which wraps
    ``clob_client.get_order_book``) inspects the payload for a
    trade timestamp and stuffs it into ``FetchResult.last_trade_ts``;
    the poller takes care of the rest.
    """
    poller = AdaptivePoller(
        fetcher=_make_fetcher({
            "T1": _ok_result("T1", last_trade_ts=time.time()),
        })
    )
    poller.add_token("T1")
    assert poller.classify("T1") == PollingTier.INACTIVE  # precondition

    await poller.tick()

    market = poller._markets["T1"]  # noqa: SLF001
    assert market.last_trade_at > 0, (
        "expected last_trade_at to be set after a fetch with last_trade_ts"
    )
    assert market.recent_trades  # the trade was appended to the deque
    # A single trade → NORMAL (not ACTIVE — below the volume threshold).
    assert poller.classify("T1") == PollingTier.NORMAL


async def test_market_activity_volume_threshold_boundary():
    """The ACTIVE volume threshold is inclusive: ≥
    ``ACTIVE_TRADE_VOLUME_THRESHOLD`` trades → ACTIVE.

    Belt-and-braces that the boundary is at the threshold itself
    (not at threshold + 1).
    """
    poller = AdaptivePoller()
    poller.add_token("T1")

    now = time.time()
    # Threshold - 1 trades → NORMAL.
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD - 1):
        poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.NORMAL

    # Threshold trades → ACTIVE.
    poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.ACTIVE


# ────────────────────────────────────────────────────────────────────────────
# Section D — Error recovery (exponential backoff on non-429 errors)
# ────────────────────────────────────────────────────────────────────────────


async def test_error_triggers_backing_off_tier():
    """A non-429 error forces the token's tier to ``BACKING_OFF``.

    After 1 error, the backoff interval is ``BASE_BACKOFF_S`` (1 s).
    The override takes precedence over the activity-based tier — a
    token with 5 recent trades would otherwise be ACTIVE (1 s), but
    after an error it MUST back off (the active tier is masked, not
    replaced).
    """
    poller = AdaptivePoller(
        fetcher=_make_fetcher({"T1": _error_result("T1")})
    )
    poller.add_token("T1")
    # Pre-promote to ACTIVE.
    now = time.time()
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.ACTIVE  # precondition

    await poller.tick()

    market = poller._markets["T1"]  # noqa: SLF001
    assert market.error_count == 1
    assert poller.classify("T1") == PollingTier.BACKING_OFF
    # 1 error → BASE_BACKOFF_S * (MULTIPLIER ** 0) = 1 s.
    assert poller.next_interval_for("T1") == BASE_BACKOFF_S


async def test_exponential_backoff_scales_with_consecutive_errors():
    """The BACKING_OFF interval scales exponentially with the
    consecutive error count: ``BASE * MULTIPLIER^(errors-1)``.

    Sequence (5 errors in a row):
      * error 1 → 1 s
      * error 2 → 2 s
      * error 3 → 4 s
      * error 4 → 8 s
      * error 5 → 16 s
    """
    responses = [_error_result("T1") for _ in range(5)]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    expected_intervals = [
        BASE_BACKOFF_S * (BACKOFF_MULTIPLIER ** i) for i in range(5)
    ]
    for i, expected in enumerate(expected_intervals):
        # force=True so each tick re-polls the token regardless of
        # the BACKING_OFF cadence (last_polled_at would otherwise
        # gate the next poll behind the backoff interval).
        await poller.tick(force=True)
        market = poller._markets["T1"]  # noqa: SLF001
        assert market.error_count == i + 1, (
            f"after {i + 1} errors, expected error_count={i + 1}; "
            f"got {market.error_count}"
        )
        actual = poller.next_interval_for("T1")
        assert actual == pytest.approx(expected, rel=1e-6), (
            f"after {i + 1} errors, expected interval={expected}s; "
            f"got {actual}s"
        )


async def test_backoff_caps_at_max():
    """The BACKING_OFF interval is capped at ``MAX_BACKOFF_S`` (60 s).

    With enough consecutive errors, the exponential would exceed the
    cap (e.g. error 7 → 64 s) — the cap kicks in.
    """
    # 7 errors → 1, 2, 4, 8, 16, 32, 64 — capped at 60 s.
    responses = [_error_result("T1") for _ in range(7)]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    for _ in range(7):
        await poller.tick(force=True)

    market = poller._markets["T1"]  # noqa: SLF001
    assert market.error_count == 7
    assert poller.next_interval_for("T1") == MAX_BACKOFF_S, (
        f"expected backoff to be capped at {MAX_BACKOFF_S}s after 7 errors; "
        f"got {poller.next_interval_for('T1')}s"
    )


async def test_error_recovery_zeroes_streak_on_first_success():
    """A single successful poll after a failure run zeroes the
    ``error_count`` streak so the tier drops back to activity-based.

    Mirrors the W24-7 ``APIResilienceLayer._record_success`` contract
    — one success is enough to confirm the upstream is healthy.
    """
    responses = [
        _error_result("T1"),
        _error_result("T1"),
        _error_result("T1"),
        _ok_result("T1"),
    ]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    await poller.tick(force=True)  # error 1
    await poller.tick(force=True)  # error 2
    await poller.tick(force=True)  # error 3

    market = poller._markets["T1"]  # noqa: SLF001
    assert market.error_count == 3
    assert poller.classify("T1") == PollingTier.BACKING_OFF

    # 4th tick: success — error_count zeroes.
    await poller.tick(force=True)
    assert market.error_count == 0, (
        f"expected error_count=0 after success; got {market.error_count}"
    )
    # No trades recorded → INACTIVE.
    assert poller.classify("T1") == PollingTier.INACTIVE


async def test_429_does_not_increment_error_count():
    """A 429 response must NOT touch ``error_count`` — the two
    streaks are independent so a 429 followed by a 5xx doesn't
    double-count the failure run."""
    responses = [
        _rate_limited_result("T1"),
        _error_result("T1"),
    ]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    await poller.tick(force=True)  # 429
    market = poller._markets["T1"]  # noqa: SLF001
    assert market.consecutive_429s == 1
    assert market.error_count == 0, (
        "429 must not touch error_count — the two streaks are independent"
    )

    await poller.tick(force=True)  # error
    assert market.consecutive_429s == 1  # NOT decremented by error
    assert market.error_count == 1
    # Error streak takes precedence? No — 429 takes precedence
    # (RATE_LIMITED is checked before BACKING_OFF in classify).
    assert poller.classify("T1") == PollingTier.RATE_LIMITED


# ────────────────────────────────────────────────────────────────────────────
# Section E — Market resolution stops polling
# ────────────────────────────────────────────────────────────────────────────


async def test_mark_resolved_removes_token_from_polling_set():
    """``mark_resolved`` removes the token from the polling set so
    the next ``tick`` skips it (no fetcher call)."""
    fetcher_calls: list[str] = []

    async def _tracking_fetcher(tid: str) -> FetchResult:
        fetcher_calls.append(tid)
        return _ok_result(tid)

    poller = AdaptivePoller(fetcher=_tracking_fetcher)
    poller.add_token("T1")
    poller.add_token("T2")

    # First tick — both tokens polled.
    await poller.tick(force=True)
    assert set(fetcher_calls) == {"T1", "T2"}
    fetcher_calls.clear()

    # Resolve T1.
    resolved_events: list[str] = []
    poller.on_resolution = lambda tid: resolved_events.append(tid)
    poller.mark_resolved("T1")

    assert "T1" not in poller.tracked_tokens
    assert resolved_events == ["T1"]
    assert poller.stats()["resolutions"] == 1

    # Second tick — only T2 polled (T1 removed).
    await poller.tick(force=True)
    assert fetcher_calls == ["T2"]


async def test_mark_resolved_is_idempotent_for_unknown_token():
    """``mark_resolved`` on an untracked token is a no-op (no hook
    fired, no resolution counter increment)."""
    poller = AdaptivePoller()
    fired: list[str] = []
    poller.on_resolution = lambda tid: fired.append(tid)

    poller.mark_resolved("NEVER_TRACKED")

    assert fired == []
    assert poller.stats()["resolutions"] == 0


async def test_resolved_flag_skipped_by_tick_even_before_removal():
    """A token whose ``MarketActivity.resolved`` flag flips to ``True``
    mid-tick is skipped — the ``tick`` snapshot filters on the flag,
    not on the token's presence in ``_markets``.

    Belt-and-braces that ``mark_resolved`` does both: flips the flag
    AND removes the token (so a parallel ``add_token`` re-adding the
    same token starts fresh, not as resolved).
    """
    async def _fetcher(tid: str) -> FetchResult:
        return _ok_result(tid)

    poller = AdaptivePoller(fetcher=_fetcher)
    poller.add_token("T1")

    # Manually flip the resolved flag WITHOUT calling mark_resolved
    # (simulates a race where the resolution hook fires mid-tick).
    poller._markets["T1"].resolved = True  # noqa: SLF001

    # tick() should skip T1 (the snapshot filters on resolved).
    results = await poller.tick()
    assert results == {}, (
        "expected tick to skip the resolved token; got "
        f"{list(results.keys())}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Section F — New-market immediate polling
# ────────────────────────────────────────────────────────────────────────────


async def test_add_token_polls_immediately_on_next_tick():
    """``add_token`` registers a token with ``last_polled_at=0`` so
    the next ``tick`` polls it immediately (no warm-up delay).

    Belt-and-braces that the ``on_new_token`` hook fires with the
    token ID, and that the token appears in ``tracked_tokens``.
    """
    new_token_events: list[str] = []

    async def _fetcher(tid: str) -> FetchResult:
        return _ok_result(tid)

    poller = AdaptivePoller(fetcher=_fetcher)
    poller.on_new_token = lambda tid: new_token_events.append(tid)

    added = poller.add_token("BRAND_NEW")
    assert added is True
    assert new_token_events == ["BRAND_NEW"]
    assert "BRAND_NEW" in poller.tracked_tokens

    # last_polled_at starts at 0 (immediate poll eligibility).
    assert poller._markets["BRAND_NEW"].last_polled_at == 0.0  # noqa: SLF001

    # tick() polls it immediately.
    results = await poller.tick()
    assert "BRAND_NEW" in results
    # last_polled_at is now non-zero (the fetch recorded a timestamp).
    assert poller._markets["BRAND_NEW"].last_polled_at > 0.0  # noqa: SLF001


async def test_add_token_is_idempotent():
    """Adding an already-tracked token is a no-op that returns
    ``False`` (no hook fired, no state change)."""
    poller = AdaptivePoller()
    fired: list[str] = []
    poller.on_new_token = lambda tid: fired.append(tid)

    assert poller.add_token("T1") is True
    assert fired == ["T1"]

    # Second add — no-op.
    assert poller.add_token("T1") is False
    assert fired == ["T1"]  # NOT called again
    assert len(poller.tracked_tokens) == 1


async def test_add_token_ignores_empty_string():
    """``add_token("")`` returns ``False`` (no token registered, no
    hook fired) — defensive against upstream callers that pass a
    placeholder / None-coerced-to-empty-string."""
    poller = AdaptivePoller()
    fired: list[str] = []
    poller.on_new_token = lambda tid: fired.append(tid)

    assert poller.add_token("") is False
    assert fired == []
    assert poller.tracked_tokens == []


async def test_record_trade_auto_registers_unknown_token():
    """``record_trade`` on an untracked token auto-registers it so
    out-of-band trade notifications (e.g. from the WS feed) don't
    require a separate ``add_token`` call."""
    poller = AdaptivePoller()

    poller.record_trade("FROM_WS", ts=time.time())

    assert "FROM_WS" in poller.tracked_tokens
    # The auto-registered token is in NORMAL tier (1 trade).
    assert poller.classify("FROM_WS") == PollingTier.NORMAL


# ────────────────────────────────────────────────────────────────────────────
# Section G — Rate-limit-aware polling (header parsing + slow-down)
# ────────────────────────────────────────────────────────────────────────────


async def test_update_rate_limit_parses_standard_headers():
    """``update_rate_limit`` parses ``X-RateLimit-Limit`` /
    ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers
    case-insensitively into the shared ``RateLimitState``."""
    poller = AdaptivePoller()
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "75",
        "X-RateLimit-Reset": str(time.time() + 60.0),
    })

    state = poller.rate_limit_state
    assert state.limit == 100
    assert state.remaining == 75
    assert state.reset_at is not None
    assert state.usage_ratio == 0.75


async def test_update_rate_limit_is_case_insensitive():
    """Header names are matched case-insensitively (some upstreams
    send lowercase, ``httpx`` normalises to title-case)."""
    poller = AdaptivePoller()
    poller.update_rate_limit({
        "x-ratelimit-limit": "200",
        "x-ratelimit-remaining": "150",
    })

    state = poller.rate_limit_state
    assert state.limit == 200
    assert state.remaining == 150
    assert state.usage_ratio == 0.75


async def test_update_rate_limit_skips_unparseable_headers():
    """Unparseable / missing headers are silently skipped (no crash,
    no state corruption). The prior values are preserved."""
    poller = AdaptivePoller()
    # Seed valid state.
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "50",
    })
    assert poller.rate_limit_state.remaining == 50

    # Garbage values — skipped, prior state preserved.
    poller.update_rate_limit({
        "X-RateLimit-Remaining": "not-a-number",
        "X-RateLimit-Limit": "",
    })
    assert poller.rate_limit_state.remaining == 50
    assert poller.rate_limit_state.limit == 100


async def test_rate_limit_critical_slowdown_quadruples_interval():
    """When ``usage_ratio < RATE_LIMIT_CRITICAL_THRESHOLD`` (10 %),
    the effective interval is 4× the tier interval (ACTIVE 1 s →
    4 s)."""
    poller = AdaptivePoller()
    poller.add_token("T1")
    # Promote to ACTIVE.
    now = time.time()
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.ACTIVE

    # Burn 95 % of the budget → usage_ratio = 0.05 (< 10 % critical).
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "5",
    })
    assert poller.rate_limit_state.usage_ratio == 0.05

    interval = poller.next_interval_for("T1")
    assert interval == ACTIVE_INTERVAL * 4.0, (
        f"expected 4× active interval ({ACTIVE_INTERVAL * 4.0}s) under "
        f"critical rate-limit pressure; got {interval}s"
    )


async def test_rate_limit_caution_slowdown_doubles_interval():
    """When ``usage_ratio < RATE_LIMIT_CAUTION_THRESHOLD`` (30 %)
    but ≥ critical (10 %), the effective interval is 2× the tier
    interval (ACTIVE 1 s → 2 s)."""
    poller = AdaptivePoller()
    poller.add_token("T1")
    now = time.time()
    for _ in range(ACTIVE_TRADE_VOLUME_THRESHOLD):
        poller.record_trade("T1", ts=now)
    assert poller.classify("T1") == PollingTier.ACTIVE

    # Burn 80 % of the budget → usage_ratio = 0.20 (≥ critical, < caution).
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "20",
    })
    assert poller.rate_limit_state.usage_ratio == 0.20

    interval = poller.next_interval_for("T1")
    assert interval == ACTIVE_INTERVAL * 2.0, (
        f"expected 2× active interval ({ACTIVE_INTERVAL * 2.0}s) under "
        f"caution rate-limit pressure; got {interval}s"
    )


async def test_rate_limit_slowdown_does_not_apply_to_backing_off_tier():
    """The rate-limit slow-down factor only applies to the
    activity-based tiers (ACTIVE / NORMAL / INACTIVE) — RATE_LIMITED
    and BACKING_OFF are already slow enough that multiplying by 4×
    would be wasteful.

    Belt-and-braces that the slow-down factor never accidentally
    pushes the BACKING_OFF interval past the ``MAX_BACKOFF_S`` cap.
    """
    poller = AdaptivePoller(
        fetcher=_make_fetcher({"T1": _error_result("T1")})
    )
    poller.add_token("T1")
    await poller.tick()
    assert poller.classify("T1") == PollingTier.BACKING_OFF

    # Apply critical rate-limit pressure.
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "1",
    })
    assert poller.rate_limit_state.usage_ratio == 0.01

    # Interval should be the BASE_BACKOFF (1 s) — NOT 1 s × 4 = 4 s.
    interval = poller.next_interval_for("T1")
    assert interval == BASE_BACKOFF_S, (
        f"BACKING_OFF interval must not be multiplied by rate-limit "
        f"slow-down; expected {BASE_BACKOFF_S}s, got {interval}s"
    )


async def test_rate_limit_state_auto_recovers_when_reset_window_passes():
    """When ``X-RateLimit-Reset`` is in the past, the budget is
    treated as refilled (``remaining`` set back to ``limit``) so the
    slow-down logic recovers immediately.

    Handles upstreams that send the ``Reset`` header but never
    refresh ``Remaining`` once the window rolls over.
    """
    poller = AdaptivePoller()
    poller.add_token("T1")
    # Burn the budget to critical.
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Reset": str(time.time() - 10.0),  # 10 s ago
    })

    state = poller.rate_limit_state
    # Auto-recovery: remaining set back to limit (100), reset_at cleared.
    assert state.remaining == 100, (
        f"expected remaining=100 after auto-recovery; got {state.remaining}"
    )
    assert state.reset_at is None
    assert state.usage_ratio == 1.0  # full budget → no slow-down


async def test_rate_limit_state_preserves_prior_values_on_partial_update():
    """A partial header set (e.g. only ``X-RateLimit-Remaining``
    refreshed, ``Limit`` constant) preserves the prior values for
    the absent headers."""
    poller = AdaptivePoller()
    # Seed full state.
    poller.update_rate_limit({
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "75",
    })

    # Only refresh Remaining.
    poller.update_rate_limit({"X-RateLimit-Remaining": "50"})

    state = poller.rate_limit_state
    assert state.limit == 100  # preserved
    assert state.remaining == 50  # updated
    assert state.usage_ratio == 0.5


async def test_rate_limit_state_usage_ratio_handles_zero_limit():
    """``usage_ratio`` returns ``None`` when ``limit`` is zero (so
    the caller doesn't divide by zero)."""
    state = RateLimitState(limit=0, remaining=0)
    assert state.usage_ratio is None


async def test_rate_limit_state_usage_ratio_handles_missing_fields():
    """``usage_ratio`` returns ``None`` when ``limit`` or
    ``remaining`` is ``None`` (the default — no headers seen yet)."""
    state = RateLimitState()
    assert state.usage_ratio is None

    state.limit = 100
    assert state.usage_ratio is None  # remaining still None

    state.remaining = 50
    assert state.usage_ratio == 0.5


async def test_fetch_result_headers_update_rate_limit_state():
    """A ``FetchResult.headers`` field updates the shared
    ``RateLimitState`` via the poller's ``_apply_result`` path
    (mirrors how the production fetcher adapter would surface the
    X-RateLimit-* headers from the HTTP response)."""
    poller = AdaptivePoller(
        fetcher=_make_fetcher({
            "T1": FetchResult(
                token_id="T1",
                ok=True,
                headers={
                    "X-RateLimit-Limit": "200",
                    "X-RateLimit-Remaining": "150",
                },
            ),
        })
    )
    poller.add_token("T1")
    await poller.tick()

    state = poller.rate_limit_state
    assert state.limit == 200
    assert state.remaining == 150
    assert state.usage_ratio == 0.75


# ────────────────────────────────────────────────────────────────────────────
# Section H — Lifecycle + stats
# ────────────────────────────────────────────────────────────────────────────


async def test_tick_without_fetcher_returns_empty_dict():
    """``tick`` on a poller without a configured fetcher returns an
    empty dict (no-op). Lets tests construct an ``AdaptivePoller``
    purely to exercise the classifier without raising."""
    poller = AdaptivePoller()  # no fetcher
    poller.add_token("T1")
    results = await poller.tick()
    assert results == {}


async def test_start_without_fetcher_raises_runtime_error():
    """``start()`` without a fetcher raises ``RuntimeError`` — the
    loop would otherwise spin uselessly."""
    poller = AdaptivePoller()  # no fetcher
    with pytest.raises(RuntimeError, match="requires a fetcher"):
        await poller.start()


async def test_stats_returns_expected_shape():
    """``stats()`` returns the documented JSON-serialisable shape."""
    poller = AdaptivePoller(
        fetcher=_make_fetcher({"T1": _ok_result("T1")}),
    )
    poller.add_token("T1")
    poller.record_trade("T1", ts=time.time())  # NORMAL tier

    await poller.tick()

    stats = poller.stats()
    assert stats["tracked_tokens"] == 1
    assert stats["polls_ok"] == 1
    assert stats["polls_failed"] == 0
    assert stats["rate_limit_events"] == 0
    assert stats["resolutions"] == 0
    assert stats["tier_counts"]["normal"] == 1
    assert stats["tier_counts"]["active"] == 0
    assert "rate_limit" in stats
    assert "intervals" in stats
    assert stats["intervals"]["active_s"] == ACTIVE_INTERVAL
    assert stats["intervals"]["normal_s"] == NORMAL_INTERVAL
    assert stats["intervals"]["inactive_s"] == INACTIVE_INTERVAL
    assert stats["intervals"]["rate_limited_s"] == RATE_LIMITED_INTERVAL


async def test_stats_counts_rate_limit_events_and_failures():
    """``stats()`` surfaces the cumulative rate-limit event count
    and the polls-failed counter (mix of 429s + non-429 errors)."""
    responses = [
        _rate_limited_result("T1"),
        _error_result("T1"),
        _ok_result("T1"),
    ]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    await poller.tick(force=True)  # 429
    await poller.tick(force=True)  # error
    await poller.tick(force=True)  # success

    stats = poller.stats()
    assert stats["polls_ok"] == 1
    assert stats["polls_failed"] == 2
    assert stats["rate_limit_events"] == 1


async def test_start_then_stop_cancels_background_task():
    """``start()`` spawns a background task; ``stop()`` cancels it
    cleanly (no pending-task warnings)."""
    async def _fetcher(tid: str) -> FetchResult:
        return _ok_result(tid)

    poller = AdaptivePoller(fetcher=_fetcher)
    poller.add_token("T1")

    await poller.start(interval=0.01)
    assert poller._running is True  # noqa: SLF001
    assert poller._task is not None  # noqa: SLF001

    # Let the loop tick at least once.
    import asyncio
    await asyncio.sleep(0.05)

    await poller.stop()
    assert poller._running is False  # noqa: SLF001
    assert poller._task is None  # noqa: SLF001


# ────────────────────────────────────────────────────────────────────────────
# Section I — Integration: end-to-end adaptive behaviour
# ────────────────────────────────────────────────────────────────────────────


async def test_end_to_end_active_to_rate_limited_to_recovery():
    """End-to-end adaptive behaviour: a token goes ACTIVE → 429 →
    RATE_LIMITED → success → still RATE_LIMITED (streak=2→1) →
    success → still RATE_LIMITED (streak=1→0) → ... → back to ACTIVE.

    This proves the per-token state machine correctly transitions
    between every tier in the spec without cross-contamination.
    """
    # Sequence: 3 OK (ACTIVE burst via last_trade_ts), 1 429,
    # then 3 OK to recover.
    now = time.time()
    responses = [
        _ok_result("T1", last_trade_ts=now),
        _ok_result("T1", last_trade_ts=now),
        _ok_result("T1", last_trade_ts=now),
        _ok_result("T1", last_trade_ts=now),
        _ok_result("T1", last_trade_ts=now),
        _rate_limited_result("T1"),
        _ok_result("T1"),  # streak 1→0 → back to ACTIVE
    ]
    poller = AdaptivePoller(fetcher=_make_fetcher(responses))
    poller.add_token("T1")

    # 5 successful polls with last_trade_ts → 5 trades recorded → ACTIVE.
    for _ in range(5):
        await poller.tick(force=True)
    assert poller.classify("T1") == PollingTier.ACTIVE, (
        "expected ACTIVE after 5 trades; "
        f"got {poller.classify('T1')}"
    )

    # 429 → RATE_LIMITED.
    await poller.tick(force=True)
    assert poller.classify("T1") == PollingTier.RATE_LIMITED
    market = poller._markets["T1"]  # noqa: SLF001
    assert market.consecutive_429s == 1

    # 1 success → streak 1→0 → back to activity tier (ACTIVE — the
    # 5 prior trades are still within the recency window because
    # only ~µs of wall clock has elapsed during the test).
    await poller.tick(force=True)
    assert market.consecutive_429s == 0
    assert poller.classify("T1") == PollingTier.ACTIVE


async def test_concurrent_polls_respect_semaphore():
    """``tick`` polls every pending token concurrently but gates on
    the configured ``max_concurrent`` semaphore.

    With ``max_concurrent=2`` and 5 pending tokens, at most 2 fetches
    are in flight at any time. The test asserts every token is
    eventually polled (the gather completes) — proving the semaphore
    doesn't deadlock.
    """
    polled: list[str] = []

    async def _slow_fetcher(tid: str) -> FetchResult:
        polled.append(tid)
        # Yield to the event loop so the semaphore is observable.
        import asyncio
        await asyncio.sleep(0)
        return _ok_result(tid)

    poller = AdaptivePoller(fetcher=_slow_fetcher, max_concurrent=2)
    for i in range(5):
        poller.add_token(f"T{i}")

    results = await poller.tick()

    # Every token was polled.
    assert len(results) == 5
    assert set(polled) == {f"T{i}" for i in range(5)}
    assert set(results.keys()) == {f"T{i}" for i in range(5)}
