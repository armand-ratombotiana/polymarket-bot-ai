#!/usr/bin/env python3
"""Aggregate system status report for the Polymarket bot platform.

Calls six backend endpoints in a single round-trip and combines them into
one JSON document plus a human-readable summary on stderr:

  GET /api/health         — liveness probe (unauthenticated)
  GET /api/status         — risk engine status (kill switch, daily P&L)
  GET /api/snapshot       — real-time portfolio snapshot (the /ws payload)
  GET /api/ml/metrics     — ML ensemble diagnostics (Brier / AUC / ECE)
  GET /api/alerts/stats   — alert counts (total / unacked / critical-unacked)
  GET /api/cache/stats    — per-cache hit/miss/size/hit_rate snapshot

This script does NOT add a new API route (per Step 3 of W12-5); it just
orchestrates the existing surface into a single aggregated view, suitable
for piping into ``jq``, posting to Slack, or feeding an external dashboard.

Usage:
  python scripts/status_report.py             # human summary + JSON
  python scripts/status_report.py --json-only  # JSON only (no summary)
  python scripts/status_report.py --no-ml     # skip the slow ML metrics call
  python scripts/status_report.py | jq .      # pretty-print the JSON

Configuration via environment variables (same as health_check.py):
  BACKEND_URL  default http://localhost:8080
  API_TOKEN    default is the dev token baked into .env

Exit codes:
  0  — all six endpoints returned 200
  1  — one or more endpoints failed; the JSON still records per-endpoint
        status so callers can see exactly which one is down.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
API_TOKEN = os.environ.get(
    "API_TOKEN",
    "I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT",
)

# (key, path, timeout_seconds, requires_auth)
ENDPOINTS: list[tuple[str, str, float, bool]] = [
    ("health", "/api/health", 5.0, False),
    ("status", "/api/status", 5.0, True),
    ("snapshot", "/api/snapshot", 8.0, True),
    ("ml_metrics", "/api/ml/metrics", 10.0, True),
    ("alerts_stats", "/api/alerts/stats", 5.0, True),
    ("cache_stats", "/api/cache/stats", 5.0, True),
]


def http_get(url: str, timeout: float = 5.0, auth: bool = True):
    """GET with optional auth header; return (status, body_or_error)."""
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except (URLError, HTTPError, TimeoutError, ConnectionError, OSError) as exc:
        return None, str(exc)


def fetch_all(skip_ml: bool = False) -> dict[str, dict[str, Any]]:
    """Hit every endpoint and return a {key: {status, ok, latency_ms, body}} map."""
    results: dict[str, dict[str, Any]] = {}
    for key, path, timeout, auth in ENDPOINTS:
        if skip_ml and key == "ml_metrics":
            results[key] = {
                "status": None,
                "ok": False,
                "latency_ms": 0,
                "body": "skipped (--no-ml)",
            }
            continue
        url = f"{BACKEND_URL}{path}"
        t0 = time.time()
        code, body = http_get(url, timeout=timeout, auth=auth)
        latency_ms = int((time.time() - t0) * 1000)
        ok = code is not None and code == 200
        results[key] = {
            "status": code,
            "ok": ok,
            "latency_ms": latency_ms,
            "body": body,
        }
    return results


def build_summary(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Distil the raw endpoint responses into a one-screen summary dict."""
    summary: dict[str, Any] = {}

    # Health
    h = results.get("health", {}).get("body")
    if isinstance(h, dict):
        summary["health"] = {
            "status": h.get("status"),
            "paper": h.get("paper"),
            "timestamp": h.get("timestamp"),
        }
    else:
        summary["health"] = {"status": "unavailable"}

    # Status (risk engine)
    s = results.get("status", {}).get("body")
    if isinstance(s, dict):
        summary["status"] = {
            "mode": s.get("mode"),
            "kill_switch_active": s.get("kill_switch_active"),
            "kill_switch_durable": s.get("kill_switch_durable"),
            "observation_only": s.get("observation_only"),
            "daily_pnl": s.get("daily_pnl"),
            "paper_balance": s.get("paper_balance"),
            "active_strategies": s.get("strategies", []),
            "seeded_markets": s.get("seeded_markets"),
            "tracked_books": s.get("tracked_books"),
        }
    else:
        summary["status"] = {"status": "unavailable"}

    # Snapshot (real-time portfolio)
    snap = results.get("snapshot", {}).get("body")
    if isinstance(snap, dict):
        summary["snapshot"] = {
            "mode": snap.get("mode"),
            "kill_switch": snap.get("kill_switch"),
            "equity": snap.get("equity"),
            "daily_pnl": snap.get("daily_pnl"),
            "open_positions": len(snap.get("positions", []) or []),
            "open_orders": len(snap.get("orders", []) or []),
            "recent_trades": len(snap.get("trades", []) or []),
        }
    else:
        summary["snapshot"] = {"status": "unavailable"}

    # ML metrics
    ml = results.get("ml_metrics", {}).get("body")
    if isinstance(ml, dict):
        summary["ml"] = {
            "model_ready": ml.get("model_ready"),
            "model_version": ml.get("model_version"),
            "brier_score": ml.get("brier_score"),
            "roc_auc": ml.get("roc_auc"),
            "log_loss": ml.get("log_loss"),
            "ece": ml.get("ece"),
            "sharpe_ratio": ml.get("sharpe_ratio"),
            "n_online_updates": ml.get("n_online_updates"),
            "drift_status": (ml.get("drift") or {}).get("status")
            if isinstance(ml.get("drift"), dict)
            else ml.get("drift"),
        }
    else:
        summary["ml"] = {"status": "unavailable"}

    # Alerts
    a = results.get("alerts_stats", {}).get("body")
    if isinstance(a, dict):
        summary["alerts"] = {
            "total": a.get("total"),
            "unacknowledged": a.get("unacknowledged"),
            "critical_unacknowledged": a.get("critical_unacknowledged"),
            "by_severity": a.get("by_severity", {}),
        }
    else:
        summary["alerts"] = {"status": "unavailable"}

    # Cache
    c = results.get("cache_stats", {}).get("body")
    if isinstance(c, dict) and isinstance(c.get("caches"), list):
        summary["cache"] = {
            "n_caches": len(c["caches"]),
            "total_hits": sum(int(x.get("hits", 0)) for x in c["caches"]),
            "total_misses": sum(int(x.get("misses", 0)) for x in c["caches"]),
            "total_size": sum(int(x.get("size", 0)) for x in c["caches"]),
            "weighted_hit_rate": _weighted_hit_rate(c["caches"]),
        }
    else:
        summary["cache"] = {"status": "unavailable"}

    return summary


def _weighted_hit_rate(caches: list[dict]) -> float:
    """Aggregate hit rate across all caches, weighted by request volume."""
    total_hits = sum(int(x.get("hits", 0)) for x in caches)
    total_misses = sum(int(x.get("misses", 0)) for x in caches)
    denom = total_hits + total_misses
    return round(total_hits / denom, 3) if denom else 0.0


def render_human(summary: dict[str, Any], results: dict[str, dict]) -> str:
    """Build a human-readable summary string (printed to stderr)."""
    lines: list[str] = []
    lines.append("=== Polymarket Bot — Aggregated Status Report ===")
    lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append(f"Backend: {BACKEND_URL}")
    lines.append("")

    # Per-endpoint availability row.
    lines.append("Endpoints:")
    for key, _, _, _ in ENDPOINTS:
        r = results.get(key, {})
        ok = r.get("ok", False)
        code = r.get("status")
        lat = r.get("latency_ms", 0)
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {key:14s} HTTP {code}  {lat}ms")
    lines.append("")

    # Health
    h = summary.get("health", {})
    lines.append(f"Health:    {h.get('status', '?')}  paper={h.get('paper')}")

    # Status
    s = summary.get("status", {})
    lines.append(
        f"Risk:      mode={s.get('mode', '?')}  "
        f"kill_switch={s.get('kill_switch_active', '?')} "
        f"(durable={s.get('kill_switch_durable', '?')})  "
        f"observation_only={s.get('observation_only', '?')}"
    )
    lines.append(
        f"           daily_pnl={s.get('daily_pnl', '?')}  "
        f"paper_balance={s.get('paper_balance', '?')}"
    )
    lines.append(
        f"           strategies={s.get('active_strategies', [])}  "
        f"seeded_markets={s.get('seeded_markets', '?')}  "
        f"tracked_books={s.get('tracked_books', '?')}"
    )

    # Snapshot
    sn = summary.get("snapshot", {})
    lines.append(
        f"Portfolio: equity={sn.get('equity', '?')}  "
        f"daily_pnl={sn.get('daily_pnl', '?')}  "
        f"open_positions={sn.get('open_positions', '?')}  "
        f"open_orders={sn.get('open_orders', '?')}  "
        f"recent_trades={sn.get('recent_trades', '?')}"
    )

    # ML
    ml = summary.get("ml", {})
    lines.append(
        f"ML:        ready={ml.get('model_ready', '?')}  "
        f"version={ml.get('model_version', '?')}  "
        f"brier={ml.get('brier_score', '?')}  "
        f"auc={ml.get('roc_auc', '?')}  "
        f"ece={ml.get('ece', '?')}  "
        f"sharpe={ml.get('sharpe_ratio', '?')}  "
        f"updates={ml.get('n_online_updates', '?')}  "
        f"drift={ml.get('drift_status', '?')}"
    )

    # Alerts
    a = summary.get("alerts", {})
    lines.append(
        f"Alerts:    total={a.get('total', '?')}  "
        f"unacked={a.get('unacknowledged', '?')}  "
        f"critical_unacked={a.get('critical_unacknowledged', '?')}"
    )

    # Cache
    c = summary.get("cache", {})
    if c.get("status") != "unavailable":
        lines.append(
            f"Cache:     caches={c.get('n_caches', '?')}  "
            f"total_size={c.get('total_size', '?')}  "
            f"hits={c.get('total_hits', '?')}  "
            f"misses={c.get('total_misses', '?')}  "
            f"weighted_hit_rate={c.get('weighted_hit_rate', '?')}"
        )
    else:
        lines.append("Cache:     unavailable")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Polymarket bot aggregated status report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--json-only",
        action="store_true",
        help="Print only the JSON report (no human-readable summary on stderr).",
    )
    p.add_argument(
        "--no-ml",
        action="store_true",
        help="Skip the /api/ml/metrics call (slow; cached for 60s server-side).",
    )
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    results = fetch_all(skip_ml=args.no_ml)
    summary = build_summary(results)

    overall_ok = all(r.get("ok", False) for r in results.values())

    report = {
        "timestamp": time.time(),
        "backend_url": BACKEND_URL,
        "overall_ok": overall_ok,
        "summary": summary,
        "endpoints": {
            key: {
                "status": r.get("status"),
                "ok": r.get("ok"),
                "latency_ms": r.get("latency_ms"),
            }
            for key, r in results.items()
        },
        "raw": {k: v.get("body") for k, v in results.items()},
    }

    if not args.json_only:
        print(render_human(summary, results), file=sys.stderr)
        print("", file=sys.stderr)

    print(json.dumps(report, indent=2, default=str))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
