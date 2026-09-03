"""
core/live_safety_gate.py — God Mode §82 Live Trading Safety Gate.

Ten-check staged validation that MUST pass before live trading is enabled.
Every check is fail-closed: an exception inside a check is recorded as a
failed check (with the exception text in ``detail``) rather than crashing
the gate — the gate's contract is to always return a verdict, never raise.

Checks (in staged order — each must pass before live trading is authorised):

  ┌────┬──────────────────────────────┬───────────────────────────────────────────┐
  │ #  │ check id                      │ pass condition                           │
  ├────┼──────────────────────────────┼───────────────────────────────────────────┤
  │ 1  │ paper_mode_24h                │ trading_mode == "paper" AND              │
  │    │                               │ (now - session_start) >= 24h             │
  │ 2  │ positive_expectancy          │ closed_positions avg_pnl > 0 (count > 0) │
  │ 3  │ max_drawdown_under_2usd       │ current drawdown < $2.00                 │
  │ 4  │ win_rate_over_50pct           │ closed_positions win_rate > 0.50         │
  │ 5  │ min_20_closed_trades          │ closed_positions count >= 20             │
  │ 6  │ ml_trained_on_real_data       │ ml_model fitted AND training_source      │
  │    │                               │ contains "real" AND n_real_samples > 0   │
  │ 7  │ drift_healthy                 │ drift_detector.drift_status == HEALTHY  │
  │ 8  │ kill_switch_tested            │ audit trail has ≥1 kill_switch_activated │
  │    │                               │ AND ≥1 kill_switch_deactivated event,    │
  │    │                               │ OR durable marker file present            │
  │ 9  │ risk_limits_verified          │ risk_manager.status_report() shows all   │
  │    │                               │ limits healthy (kill switch clear,        │
  │    │                               │ exposure reconciled, within all caps)    │
  │ 10 │ api_credentials_configured    │ settings.has_credentials AND             │
  │    │                               │ settings.has_api_keys                    │
  └────┴──────────────────────────────┴───────────────────────────────────────────┘

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose:

  GET  /api/live/readiness   — run all 10 checks, return {passed, checks, ...}
  POST /api/live/enable      — attempt to flip live mode on (only succeeds if
                               all 10 checks pass; requires confirm=true)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Request

log = logging.getLogger(__name__)

# ── God Mode §82 thresholds ─────────────────────────────────────────────────
# Centralised so the dashboard / tests can introspect the exact bar the gate
# holds live trading to. None of these are tunable at runtime on purpose —
# live authorisation is a deliberately high, stable bar.
PAPER_MODE_MIN_SECONDS: float = 24.0 * 3600.0   # 24h continuous paper session
MIN_CLOSED_TRADES: int = 20
MIN_WIN_RATE: float = 0.50
MAX_LIVE_DRAWDOWN_USD: float = 2.0
DRIFT_HEALTHY_STATUS: str = "HEALTHY"

# ── Durable marker for an operator-asserted kill-switch test ────────────────
# Optional override for check #8: if the audit trail doesn't yet carry the
# activate→deactivate evidence (e.g. fresh deploy where the switch was
# exercised before audit logging began, or audit DB rotated), an operator can
# ``touch`` this marker file to assert "kill switch has been tested". The
# audit-trail path remains the primary signal — this is a documented escape
# hatch, not the default.
KILL_SWITCH_TESTED_PATH: Path = Path(
    os.environ.get(
        "LIVE_SAFETY_KILL_SWITCH_TESTED_PATH",
        "/app/data/live_safety_kill_switch_tested",
    )
)

# ── Stable check identifiers (dashboards / tests rely on these) ─────────────
CHECK_PAPER_MODE = "paper_mode_24h"
CHECK_POSITIVE_EXPECTANCY = "positive_expectancy"
CHECK_MAX_DRAWDOWN = "max_drawdown_under_2usd"
CHECK_WIN_RATE = "win_rate_over_50pct"
CHECK_CLOSED_TRADES = "min_20_closed_trades"
CHECK_ML_REAL_DATA = "ml_trained_on_real_data"
CHECK_DRIFT_HEALTHY = "drift_healthy"
CHECK_KILL_SWITCH_TESTED = "kill_switch_tested"
CHECK_RISK_LIMITS = "risk_limits_verified"
CHECK_API_CREDENTIALS = "api_credentials_configured"

# Staged evaluation order — checks are presented in this exact sequence in the
# report so an operator reads top-to-bottom: paper-mode soak → performance
# evidence → ML governance → safety posture → credentials.
CHECK_ORDER: tuple[str, ...] = (
    CHECK_PAPER_MODE,
    CHECK_POSITIVE_EXPECTANCY,
    CHECK_MAX_DRAWDOWN,
    CHECK_WIN_RATE,
    CHECK_CLOSED_TRADES,
    CHECK_ML_REAL_DATA,
    CHECK_DRIFT_HEALTHY,
    CHECK_KILL_SWITCH_TESTED,
    CHECK_RISK_LIMITS,
    CHECK_API_CREDENTIALS,
)

SEVERITY_BLOCKING = "BLOCKING"   # every check is blocking until it passes


# ── Public API ───────────────────────────────────────────────────────────────

async def check_live_readiness() -> dict[str, Any]:
    """
    Run all 10 God Mode §82 staged checks and return a verdict dict::

        {
            "passed": bool,                  # True only if ALL 10 checks pass
            "checks": [ {id, name, passed, severity, threshold, value, detail}, ... ],
            "passed_count": int,
            "total_count": int,
            "blocking_checks": [str, ...],   # ids of failed checks (empty if passed)
            "checked_at": float,             # epoch seconds
        }

    Never raises — a check that throws records itself as failed (with the
    exception text in ``detail``) so the gate always returns a verdict. This
    is the live-trading safety contract: the gate must answer, even when a
    dependency is broken.
    """
    checks: list[dict[str, Any]] = []
    checks.append(await _check_paper_mode())
    checks.append(await _check_positive_expectancy())
    checks.append(await _check_max_drawdown())
    checks.append(await _check_win_rate())
    checks.append(await _check_closed_trades())
    checks.append(await _check_ml_real_data())
    checks.append(await _check_drift_healthy())
    checks.append(await _check_kill_switch_tested())
    checks.append(await _check_risk_limits())
    checks.append(await _check_api_credentials())

    passed_count = sum(1 for c in checks if c.get("passed"))
    blocking = [c["id"] for c in checks if not c.get("passed")]
    return {
        "passed": len(blocking) == 0,
        "checks": checks,
        "passed_count": passed_count,
        "total_count": len(checks),
        "blocking_checks": blocking,
        "checked_at": time.time(),
    }


async def get_live_safety_report() -> dict[str, Any]:
    """
    Richer report wrapping ``check_live_readiness`` with mode context,
    thresholds, and operator guidance. The shape::

        {
            "generated_at": float,
            "gate": "God Mode §82 — Live Trading Safety Gate",
            "readiness": { … check_live_readiness output … },
            "mode_context": {
                "trading_mode": str,
                "paper_trade": bool,
                "live_trading_enabled": bool,
                "has_credentials": bool,
                "has_api_keys": bool,
                "kill_switch_active": bool,
                "kill_switch_durable": bool,
            },
            "thresholds": { … §82 constants … },
            "guidance": str,
        }

    Designed as the single payload an operator dashboard polls to render the
    full "are we ready to go live?" posture.
    """
    readiness = await check_live_readiness()

    mode_context: dict[str, Any]
    try:
        from config import settings
        from core.data_store import store
        from core.safety import kill_switch_file_exists

        mode_context = {
            "trading_mode": settings.trading_mode,
            "paper_trade": bool(settings.paper_trade),
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "has_credentials": bool(settings.has_credentials),
            "has_api_keys": bool(settings.has_api_keys),
            "kill_switch_active": bool(getattr(store, "kill_switch_active", False)),
            "kill_switch_durable": bool(kill_switch_file_exists()),
        }
    except Exception as e:
        log.error("[live_safety_gate] mode context gathering failed: %s", e)
        mode_context = {"error": f"mode context unavailable: {e}"}

    return {
        "generated_at": time.time(),
        "gate": "God Mode §82 — Live Trading Safety Gate",
        "readiness": readiness,
        "mode_context": mode_context,
        "thresholds": {
            "paper_mode_min_seconds": PAPER_MODE_MIN_SECONDS,
            "min_closed_trades": MIN_CLOSED_TRADES,
            "min_win_rate": MIN_WIN_RATE,
            "max_live_drawdown_usd": MAX_LIVE_DRAWDOWN_USD,
            "drift_healthy_status": DRIFT_HEALTHY_STATUS,
            "check_order": list(CHECK_ORDER),
        },
        "guidance": (
            "All 10 staged checks must pass before live trading is enabled. "
            "POST /api/live/enable with confirm=true to attempt activation; "
            "the endpoint refuses and returns HTTP 409 with the blocking "
            "check list if any check fails. The in-memory mode flip is "
            "supplemented (not replaced) by setting TRADING_MODE=live + "
            "LIVE_TRADING_ENABLED=true in .env and restarting for durable "
            "activation across process restarts."
        ),
    }


# ── Individual staged checks ────────────────────────────────────────────────
# Each returns a dict: {id, name, passed, severity, threshold, value, detail}.
# Local imports keep a broken dependency from crashing the whole gate — the
# try/except envelope converts any failure into a recorded failed check.

async def _check_paper_mode() -> dict[str, Any]:
    """Check #1: paper_mode ≥ 24h — current continuous paper session length."""
    name = "Paper mode soak ≥ 24h"
    threshold = f"trading_mode=='paper' AND session_age_s >= {PAPER_MODE_MIN_SECONDS:.0f}"
    try:
        from config import settings
        from core.data_store import store

        mode = settings.trading_mode
        session_start = float(getattr(store, "session_start", time.time()))
        age_s = max(0.0, time.time() - session_start)
        in_paper = mode == "paper"
        passed = in_paper and age_s >= PAPER_MODE_MIN_SECONDS
        if passed:
            detail = f"paper session running {age_s/3600.0:.2f}h (≥24h)"
        elif not in_paper:
            detail = (
                f"not currently in paper mode (trading_mode={mode!r}); "
                f"the §82 gate requires a continuous ≥24h paper soak first. "
                f"Note: session_start resets on every process restart, so the "
                f"measured age reflects only the current continuous session."
            )
        else:
            detail = (
                f"paper session only {age_s/3600.0:.2f}h old "
                f"(need ≥{PAPER_MODE_MIN_SECONDS/3600.0:.0f}h); "
                f"session_start resets on restart."
            )
        return {
            "id": CHECK_PAPER_MODE, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {"trading_mode": mode, "session_age_seconds": round(age_s, 1)},
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] paper_mode check failed: %s", e)
        return _failed(CHECK_PAPER_MODE, name, threshold, f"check raised: {e}")


async def _check_positive_expectancy() -> dict[str, Any]:
    """Check #2: positive expectancy — avg PnL per closed trade > 0."""
    name = "Positive expectancy (avg PnL > 0)"
    threshold = "closed_positions.avg_pnl > 0 (count > 0)"
    try:
        from core.closed_positions import closed_positions

        stats = await closed_positions.get_closed_stats()
        count = int(stats.get("count", 0))
        avg_pnl = float(stats.get("avg_pnl", 0.0) or 0.0)
        # Expectancy = mean PnL per trade = (win_rate*avg_win)+(loss_rate*avg_loss).
        # stats.avg_pnl is exactly this (total_pnl/count), so the >0 test is the
        # canonical positive-expectancy gate.
        passed = count > 0 and avg_pnl > 0.0
        if passed:
            detail = f"avg_pnl=${avg_pnl:+.4f} across {count} closed trade(s) — positive expectancy"
        elif count == 0:
            detail = "no closed trades yet — expectancy undefined until ≥1 trade closes"
        else:
            detail = f"avg_pnl=${avg_pnl:+.4f} across {count} closed trade(s) — non-positive expectancy"
        return {
            "id": CHECK_POSITIVE_EXPECTANCY, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {"count": count, "avg_pnl": round(avg_pnl, 4)},
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] positive_expectancy check failed: %s", e)
        return _failed(CHECK_POSITIVE_EXPECTANCY, name, threshold, f"check raised: {e}")


async def _check_max_drawdown() -> dict[str, Any]:
    """Check #3: current drawdown < $2.00 (stricter than the $8 hard breaker)."""
    name = "Max drawdown < $2.00"
    threshold = f"drawdown_dollars < {MAX_LIVE_DRAWDOWN_USD:.2f}"
    try:
        report = await _risk_status_report()
        dd = float(report.get("drawdown_dollars", 0.0) or 0.0)
        passed = dd < MAX_LIVE_DRAWDOWN_USD
        if passed:
            detail = f"current drawdown ${dd:.2f} < ${MAX_LIVE_DRAWDOWN_USD:.2f} live gate"
        else:
            detail = (
                f"current drawdown ${dd:.2f} >= ${MAX_LIVE_DRAWDOWN_USD:.2f} live gate "
                f"(risk engine hard breaker is ${report.get('max_drawdown_limit', 0.0):.2f})"
            )
        return {
            "id": CHECK_MAX_DRAWDOWN, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {
                "drawdown_dollars": round(dd, 4),
                "max_drawdown_limit": float(report.get("max_drawdown_limit", 0.0) or 0.0),
            },
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] max_drawdown check failed: %s", e)
        return _failed(CHECK_MAX_DRAWDOWN, name, threshold, f"check raised: {e}")


async def _check_win_rate() -> dict[str, Any]:
    """Check #4: win rate > 50%."""
    name = "Win rate > 50%"
    threshold = f"closed_positions.win_rate > {MIN_WIN_RATE:.2f}"
    try:
        from core.closed_positions import closed_positions

        stats = await closed_positions.get_closed_stats()
        count = int(stats.get("count", 0))
        win_rate = float(stats.get("win_rate", 0.0) or 0.0)
        passed = count > 0 and win_rate > MIN_WIN_RATE
        if passed:
            detail = f"win_rate={win_rate*100:.2f}% across {count} closed trade(s)"
        elif count == 0:
            detail = "no closed trades yet — win rate undefined"
        else:
            detail = f"win_rate={win_rate*100:.2f}% (need > {MIN_WIN_RATE*100:.0f}%)"
        return {
            "id": CHECK_WIN_RATE, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {"count": count, "win_rate": round(win_rate, 4)},
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] win_rate check failed: %s", e)
        return _failed(CHECK_WIN_RATE, name, threshold, f"check raised: {e}")


async def _check_closed_trades() -> dict[str, Any]:
    """Check #5: ≥ 20 closed trades (statistical significance floor)."""
    name = "≥ 20 closed trades"
    threshold = f"closed_positions.count >= {MIN_CLOSED_TRADES}"
    try:
        from core.closed_positions import closed_positions

        stats = await closed_positions.get_closed_stats()
        count = int(stats.get("count", 0))
        passed = count >= MIN_CLOSED_TRADES
        if passed:
            detail = f"{count} closed trade(s) (≥ {MIN_CLOSED_TRADES})"
        else:
            detail = f"{count} closed trade(s) — need ≥ {MIN_CLOSED_TRADES} for statistical significance"
        return {
            "id": CHECK_CLOSED_TRADES, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {"count": count},
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] closed_trades check failed: %s", e)
        return _failed(CHECK_CLOSED_TRADES, name, threshold, f"check raised: {e}")


async def _check_ml_real_data() -> dict[str, Any]:
    """Check #6: ML model trained on real data (not synthetic-only)."""
    name = "ML trained on real data"
    threshold = "ml_model.is_fitted AND training_source contains 'real' AND n_real_samples > 0"
    try:
        from ml.model import ml_model

        is_fitted = bool(getattr(ml_model, "is_fitted", False))
        training_source = str(getattr(ml_model, "training_source", "synthetic_only"))
        n_real = int(getattr(ml_model, "n_real_samples", 0) or 0)
        # ``fit_initial`` sets training_source="real_and_synthetic" when the
        # TimescaleDB history had ≥200 real samples; "synthetic_only" otherwise.
        has_real = "real" in training_source and n_real > 0
        passed = is_fitted and has_real
        if passed:
            detail = (
                f"model fitted on {n_real} real + "
                f"{getattr(ml_model, 'n_synthetic_samples', 0)} synthetic samples "
                f"(source={training_source})"
            )
        elif not is_fitted:
            detail = "ml_model not fitted — train on real market history before going live"
        else:
            detail = (
                f"model trained on synthetic-only data (source={training_source}, "
                f"n_real_samples={n_real}) — §82 requires real-data training"
            )
        return {
            "id": CHECK_ML_REAL_DATA, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {
                "is_fitted": is_fitted, "training_source": training_source,
                "n_real_samples": n_real,
                "n_synthetic_samples": int(getattr(ml_model, "n_synthetic_samples", 0) or 0),
            },
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] ml_real_data check failed: %s", e)
        return _failed(CHECK_ML_REAL_DATA, name, threshold, f"check raised: {e}")


async def _check_drift_healthy() -> dict[str, Any]:
    """Check #7: ML drift detector reports HEALTHY status."""
    name = "ML drift status HEALTHY"
    threshold = f"drift_detector.drift_status == {DRIFT_HEALTHY_STATUS!r}"
    try:
        from ml.drift_detector import drift_detector

        status = str(getattr(drift_detector, "drift_status", "UNKNOWN"))
        passed = status == DRIFT_HEALTHY_STATUS
        if passed:
            detail = (
                f"drift_status=HEALTHY (PSI={drift_detector.last_psi}, "
                f"KS={drift_detector.last_ks_stat}, "
                f"rolling_brier={drift_detector.rolling_brier})"
            )
        else:
            detail = (
                f"drift_status={status} (need HEALTHY) — PSI={drift_detector.last_psi}, "
                f"KS={drift_detector.last_ks_stat}, "
                f"rolling_brier={drift_detector.rolling_brier}; "
                f"retrain before going live"
            )
        return {
            "id": CHECK_DRIFT_HEALTHY, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {
                "drift_status": status,
                "psi": float(drift_detector.last_psi),
                "ks_stat": float(drift_detector.last_ks_stat),
                "rolling_brier": drift_detector.rolling_brier,
            },
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] drift_healthy check failed: %s", e)
        return _failed(CHECK_DRIFT_HEALTHY, name, threshold, f"check raised: {e}")


async def _check_kill_switch_tested() -> dict[str, Any]:
    """Check #8: kill switch has been exercised (activated then deactivated).

    Primary signal: the audit trail carries at least one
    ``kill_switch_activated`` AND at least one ``kill_switch_deactivated``
    event (proving the operator ran the full activate→deactivate cycle, so the
    durable marker file + in-memory flag + cancel-all-orders path are all
    known-good).

    Escape hatch: a durable marker file at ``KILL_SWITCH_TESTED_PATH`` lets an
    operator assert "tested" when the audit evidence is unavailable (e.g. on a
    fresh deploy where the switch was exercised before audit logging began or
    the audit DB was rotated). The marker is the documented override, not the
    default — the audit trail is the canonical evidence path.
    """
    name = "Kill switch tested (activate→deactivate exercised)"
    threshold = "audit trail has ≥1 kill_switch_activated AND ≥1 kill_switch_deactivated, OR marker file present"
    try:
        # Escape hatch first — cheap filesystem check.
        marker_present = False
        try:
            marker_present = KILL_SWITCH_TESTED_PATH.exists()
        except OSError:
            marker_present = False

        activated_count = 0
        deactivated_count = 0
        last_activated_ts: float | None = None
        last_deactivated_ts: float | None = None
        try:
            from core.audit_logger import audit_logger

            # Pull a wide window of risk-category events so we don't miss an
            # early-cycle test buried under later risk events. 500 is generous
            # but bounded — the audit DB is append-only and risk events are
            # rare (only fire on breaker trips / manual resets).
            events = await audit_logger.get_recent_events(limit=500, category="risk")
            for ev in events:
                et = str(ev.get("event_type", "") or "")
                if et == "kill_switch_activated":
                    activated_count += 1
                    ts = ev.get("timestamp")
                    if ts is not None and (last_activated_ts is None or float(ts) > last_activated_ts):
                        last_activated_ts = float(ts)
                elif et == "kill_switch_deactivated":
                    deactivated_count += 1
                    ts = ev.get("timestamp")
                    if ts is not None and (last_deactivated_ts is None or float(ts) > last_deactivated_ts):
                        last_deactivated_ts = float(ts)
        except Exception as e:
            log.debug("[live_safety_gate] audit trail inspection failed: %s", e)

        audit_evidence = activated_count >= 1 and deactivated_count >= 1
        passed = audit_evidence or marker_present

        if passed and audit_evidence:
            ordered = (
                last_activated_ts is not None
                and last_deactivated_ts is not None
                and last_deactivated_ts >= last_activated_ts
            )
            detail = (
                f"audit trail: {activated_count} activation(s), "
                f"{deactivated_count} deactivation(s); "
                f"last deactivate {'followed' if ordered else 'preceded'} last activate"
            )
        elif passed and marker_present:
            detail = (
                f"operator marker file present at {KILL_SWITCH_TESTED_PATH} "
                f"(audit trail had {activated_count} activate / {deactivated_count} "
                f"deactivate — marker is the documented override)"
            )
        else:
            detail = (
                f"audit trail has {activated_count} activate / {deactivated_count} "
                f"deactivate event(s); need ≥1 of each. Exercise the kill switch "
                f"(activate via POST /api/risk/kill-switch, then reset via "
                f"DELETE /api/risk/kill-switch) or create the marker file at "
                f"{KILL_SWITCH_TESTED_PATH}."
            )
        return {
            "id": CHECK_KILL_SWITCH_TESTED, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {
                "audit_activated_count": activated_count,
                "audit_deactivated_count": deactivated_count,
                "last_activated_at": last_activated_ts,
                "last_deactivated_at": last_deactivated_ts,
                "marker_file_present": marker_present,
                "marker_path": str(KILL_SWITCH_TESTED_PATH),
            },
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] kill_switch_tested check failed: %s", e)
        return _failed(CHECK_KILL_SWITCH_TESTED, name, threshold, f"check raised: {e}")


async def _check_risk_limits() -> dict[str, Any]:
    """Check #9: all risk limits verified healthy (kill switch clear, exposure reconciled, within caps)."""
    name = "Risk limits verified"
    threshold = "kill_switch clear AND exposure_reconciled AND within all caps AND no breakers tripped"
    try:
        report = await _risk_status_report()

        sub_checks: list[dict[str, Any]] = [
            {"key": "kill_switch", "ok": not bool(report.get("kill_switch", True)),
             "value": report.get("kill_switch")},
            {"key": "kill_switch_durable", "ok": not bool(report.get("kill_switch_durable", True)),
             "value": report.get("kill_switch_durable")},
            {"key": "observation_only", "ok": not bool(report.get("observation_only", False)),
             "value": report.get("observation_only")},
            {"key": "exposure_reconciled", "ok": bool(report.get("exposure_reconciled", False)),
             "value": report.get("exposure_reconciled")},
            {"key": "drawdown_within_live_gate",
             "ok": float(report.get("drawdown_dollars", 0.0) or 0.0) < MAX_LIVE_DRAWDOWN_USD,
             "value": report.get("drawdown_dollars"),
             "threshold": MAX_LIVE_DRAWDOWN_USD},
            {"key": "drawdown_within_hard_limit",
             "ok": float(report.get("drawdown_dollars", 0.0) or 0.0) < float(report.get("max_drawdown_limit", 0.0) or 0.0),
             "value": report.get("drawdown_dollars"),
             "threshold": report.get("max_drawdown_limit")},
            {"key": "daily_loss_within_stop",
             "ok": float(report.get("daily_pnl", 0.0) or 0.0) > float(report.get("daily_loss_limit", 0.0) or 0.0),
             "value": report.get("daily_pnl"),
             "threshold": report.get("daily_loss_limit")},
            {"key": "weekly_loss_within_stop",
             "ok": float(report.get("weekly_pnl", 0.0) or 0.0) > float(report.get("weekly_loss_limit", 0.0) or 0.0),
             "value": report.get("weekly_pnl"),
             "threshold": report.get("weekly_loss_limit")},
            {"key": "total_exposure_within_cap",
             "ok": float(report.get("total_exposure", 0.0) or 0.0) <= float(report.get("max_total_exposure", 0.0) or 0.0),
             "value": report.get("total_exposure"),
             "threshold": report.get("max_total_exposure")},
            {"key": "pending_capital_within_cap",
             "ok": float(report.get("pending_order_capital", 0.0) or 0.0) <= float(report.get("max_pending_order_capital", 0.0) or 0.0),
             "value": report.get("pending_order_capital"),
             "threshold": report.get("max_pending_order_capital")},
            {"key": "open_orders_within_cap",
             "ok": int(report.get("open_orders", 0) or 0) <= int(report.get("max_open_orders", 0) or 0),
             "value": report.get("open_orders"),
             "threshold": report.get("max_open_orders")},
        ]
        failed_subs = [s["key"] for s in sub_checks if not s["ok"]]
        passed = len(failed_subs) == 0
        if passed:
            detail = "all risk sub-limits healthy (kill switch clear, exposure reconciled, within every cap)"
        else:
            detail = f"risk sub-limits failing: {failed_subs}"
        return {
            "id": CHECK_RISK_LIMITS, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {
                "failed_sub_checks": failed_subs,
                "sub_checks": sub_checks,
                "kill_switch": report.get("kill_switch"),
                "observation_only": report.get("observation_only"),
                "exposure_reconciled": report.get("exposure_reconciled"),
                "drawdown_dollars": report.get("drawdown_dollars"),
                "total_exposure": report.get("total_exposure"),
            },
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] risk_limits check failed: %s", e)
        return _failed(CHECK_RISK_LIMITS, name, threshold, f"check raised: {e}")


async def _check_api_credentials() -> dict[str, Any]:
    """Check #10: API credentials configured (wallet key + CLOB API key/secret/passphrase)."""
    name = "API credentials configured"
    threshold = "settings.has_credentials AND settings.has_api_keys"
    try:
        from config import settings

        has_creds = bool(settings.has_credentials)
        has_keys = bool(settings.has_api_keys)
        passed = has_creds and has_keys
        if passed:
            detail = "wallet private key + CLOB API key/secret/passphrase all configured"
        else:
            missing: list[str] = []
            if not has_creds:
                missing.append("POLY_PRIVATE_KEY")
            if not has_keys:
                missing.append("POLY_API_KEY/POLY_API_SECRET/POLY_API_PASSPHRASE")
            detail = f"missing credentials: {missing} — set in .env before going live"
        return {
            "id": CHECK_API_CREDENTIALS, "name": name, "passed": passed,
            "severity": SEVERITY_BLOCKING, "threshold": threshold,
            "value": {"has_credentials": has_creds, "has_api_keys": has_keys},
            "detail": detail,
        }
    except Exception as e:
        log.error("[live_safety_gate] api_credentials check failed: %s", e)
        return _failed(CHECK_API_CREDENTIALS, name, threshold, f"check raised: {e}")


# ── FastAPI route registration ───────────────────────────────────────────────

# ── FastAPI request model (module-level so FastAPI resolves it as a body) ───
# Mirrors the convention in ``api/server.py`` where every Pydantic request model
# is defined at module scope (e.g. ``ObservationModeRequest``). Defining the
# model inside ``register_routes`` made FastAPI treat the parameter as a query
# arg (``loc: ["query","req"]``) instead of a JSON body.

try:
    from pydantic import BaseModel, Field

    class EnableLiveRequest(BaseModel):
        """Request body for ``POST /api/live/enable``."""

        confirm: bool = Field(
            default=False,
            description="Must be true to authorise live-mode activation (defence against accidental clicks).",
        )
        reason: str = Field(
            default="",
            description="Operator justification for enabling live trading (recorded in the audit trail).",
        )
except Exception:  # pragma: no cover — pydantic is a hard project dep; this only trips in odd unit-test stubs
    EnableLiveRequest = None  # type: ignore[assignment]


# ── FastAPI route registration ───────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append live-safety-gate endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/live/readiness
          Run all 10 God Mode §82 staged checks and return the verdict dict
          ({passed, checks, passed_count, total_count, blocking_checks,
          checked_at}). Never 500s — a check that throws records itself as
          failed.

      POST /api/live/enable
          Request body: {confirm: bool, reason: str}
          Attempt to flip the bot into live trading mode. Requires
          ``confirm=true`` (defence against accidental double-click). Runs
          ``check_live_readiness`` first; if any check fails, returns HTTP 409
          with the blocking-check list and the full readiness payload. On
          success, flips the in-memory mode flags (live_trading_enabled=True,
          trading_mode="live", paper_trade=False) and logs an audit event.

          NOTE: the in-memory flip is sufficient for ``check_order`` to start
          admitting live orders, but it does NOT persist across process
          restarts. For durable activation, set TRADING_MODE=live and
          LIVE_TRADING_ENABLED=true in .env and restart — the response
          payload carries this guidance.

    Rate limiting (W10-4)
    ---------------------
    ``POST /api/live/enable`` is rate-limited at ``3/minute`` — the strictest
    tier in the W10-4 policy (one-shot escalation, no operator should be
    able to spam the gate into flipping live mode). The shared ``limiter``
    singleton is imported lazily from ``api.rate_limit`` (a tiny shared
    module that exists specifically to break what would otherwise be a
    circular import between this module and ``api.server``). The
    ``request: Request`` parameter is required by slowapi's decorator at
    function-definition time even when the limiter is disabled (e.g. in
    the test suite).
    """
    from fastapi import HTTPException

    from api.rate_limit import LIVE_ENABLE_LIMIT, limiter

    @app.get("/api/live/readiness", tags=["live"])
    async def _live_readiness():
        """Run all 10 §82 staged checks; return {passed, checks, blocking_checks, ...}."""
        return await check_live_readiness()

    @app.post("/api/live/enable", tags=["live"])
    @limiter.limit(LIVE_ENABLE_LIMIT)
    async def _enable_live(request: Request, req: "EnableLiveRequest"):
        """Attempt to enable live trading. Refuses (HTTP 409) if any §82 check fails."""
        if not req.confirm:
            raise HTTPException(
                status_code=400,
                detail="confirm=true is required to enable live trading (defence against accidental activation).",
            )
        readiness = await check_live_readiness()
        if not readiness["passed"]:
            # 409 Conflict: the request conflicts with the current safety state.
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Live trading NOT enabled — God Mode §82 safety gate failed.",
                    "passed_count": readiness["passed_count"],
                    "total_count": readiness["total_count"],
                    "blocking_checks": readiness["blocking_checks"],
                    "checks": readiness["checks"],
                    "guidance": "Resolve every blocking check, then re-POST /api/live/enable.",
                },
            )
        # All 10 checks passed — authorise live trading in-memory.
        try:
            from config import settings
            from core.audit_logger import audit_logger
            from core.data_store import store

            settings.live_trading_enabled = True
            settings.trading_mode = "live"
            settings.paper_trade = False
            await audit_logger.log_event(
                category="system",
                event_type="live_trading_enabled",
                details=(
                    f"Live trading enabled via §82 safety gate "
                    f"(reason={req.reason or 'operator request'}). "
                    f"In-memory flags flipped — set TRADING_MODE=live + "
                    f"LIVE_TRADING_ENABLED=true in .env and restart for "
                    f"durable activation."
                ),
            )
            await store.log_event(
                f"🔴 LIVE TRADING ENABLED via §82 safety gate — reason: {req.reason or 'operator request'}"
            )
            log.warning(
                "[live_safety_gate] 🔴 LIVE TRADING ENABLED via safety gate — reason=%s",
                req.reason or "operator request",
            )
            return {
                "enabled": True,
                "mode": settings.trading_mode,
                "live_trading_enabled": settings.live_trading_enabled,
                "paper_trade": settings.paper_trade,
                "readiness": readiness,
                "note": (
                    "In-memory mode flags flipped — check_order will now admit live orders. "
                    "Set TRADING_MODE=live + LIVE_TRADING_ENABLED=true in .env and restart "
                    "for durable activation across process restarts."
                ),
            }
        except Exception as e:
            # W15-6 (OWASP A02 — Cryptographic Failures / Information
            # Disclosure): the raw exception message MUST NOT be reflected to
            # the client — it can leak internal paths / class names / SQL
            # fragments. The full traceback is logged server-side (the
            # ``log.error`` call below) so the operator can debug; the client
            # sees only a generic 500.
            log.error(
                "[live_safety_gate] live enable failed post-gate: %s",
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "All §82 checks passed but the in-memory mode flip "
                    "failed — see server logs for details (request_id "
                    "in the X-Request-ID response header)."
                ),
            )


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _risk_status_report() -> dict[str, Any]:
    """Fetch the risk manager's status report, swallowing failures (check #3/#9 use this)."""
    try:
        from risk.manager import risk_manager
        return await risk_manager.status_report()
    except Exception as e:
        log.debug("[live_safety_gate] risk_manager.status_report unavailable: %s", e)
        return {}


def _failed(check_id: str, name: str, threshold: str, detail: str) -> dict[str, Any]:
    """Construct a failed-check payload (used by every check's except branch)."""
    return {
        "id": check_id,
        "name": name,
        "passed": False,
        "severity": SEVERITY_BLOCKING,
        "threshold": threshold,
        "value": None,
        "detail": detail,
    }


__all__ = [
    "check_live_readiness",
    "get_live_safety_report",
    "register_routes",
    "CHECK_ORDER",
    "CHECK_PAPER_MODE",
    "CHECK_POSITIVE_EXPECTANCY",
    "CHECK_MAX_DRAWDOWN",
    "CHECK_WIN_RATE",
    "CHECK_CLOSED_TRADES",
    "CHECK_ML_REAL_DATA",
    "CHECK_DRIFT_HEALTHY",
    "CHECK_KILL_SWITCH_TESTED",
    "CHECK_RISK_LIMITS",
    "CHECK_API_CREDENTIALS",
    "PAPER_MODE_MIN_SECONDS",
    "MIN_CLOSED_TRADES",
    "MIN_WIN_RATE",
    "MAX_LIVE_DRAWDOWN_USD",
    "DRIFT_HEALTHY_STATUS",
    "KILL_SWITCH_TESTED_PATH",
]
