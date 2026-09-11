#!/usr/bin/env python3
"""Render a human-readable performance report from the live profiler.

W15-4 — Performance report script.

Calls the live ``GET /api/profiling/stats`` and ``GET
/api/profiling/slowest`` endpoints on the polymarket-bot API server and
renders a human-readable report that:

  * Lists the slowest endpoints (top-N by p95 latency).
  * Flags endpoints that exceed the configured latency targets
    (defaults: p95 < 200 ms, p99 < 500 ms, error rate < 1 %).
  * Prints actionable recommendations tied to each flag (cache the
    route, investigate error spikes, batch N+1 queries …).

This script is purely a client of the API — it never imports the
production code path (no DB connections, no ML ensemble, no risk
manager). That means it can run from any host that has network access
to the bot's API port, including a developer laptop pointed at a
staging deployment.

Usage
-----
Run from the project root::

    python -m scripts.perf_report
    python -m scripts.perf_report --limit 20 --p95-target-ms 150
    python -m scripts.perf_report --url http://bot.local:8080 --token "$API_TOKEN"
    python -m scripts.perf_report --reset        # wipe stats after reading

Exit codes
----------
  * ``0`` — report rendered; no endpoint exceeded a latency / error target.
  * ``1`` — one or more endpoints exceeded a target (action required).
  * ``2`` — couldn't reach the API server (network / auth / 5xx).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    # ``httpx`` is already in the project's requirements (the CLI uses it
    # too — see ``mini-services/polymarket-bot/cli.py``); fall back to
    # ``urllib.request`` if it's missing so the script still runs on a
    # stripped-down operator workstation.
    import httpx  # type: ignore[import-not-found]
    _HAS_HTTPX = True
except ImportError:  # pragma: no cover — exercised only on stripped envs
    import urllib.request
    import urllib.error
    _HAS_HTTPX = False


# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_URL = os.environ.get("BOT_API_URL", "http://localhost:8080")
DEFAULT_TOKEN = os.environ.get("API_TOKEN") or os.environ.get("BOT_API_TOKEN", "")
DEFAULT_LIMIT = 15
DEFAULT_P95_TARGET_MS = 200.0
DEFAULT_P99_TARGET_MS = 500.0
DEFAULT_ERROR_RATE_TARGET_PCT = 1.0


def _fetch_json(url: str, token: str, *, method: str = "GET", timeout: float = 10.0) -> Any:
    """Fetch JSON from ``url`` with bearer auth. Raises ``RuntimeError`` on failure."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if _HAS_HTTPX:
        with httpx.Client(timeout=timeout) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            elif method == "POST":
                resp = client.post(url, headers=headers)
            else:
                raise ValueError(f"unsupported method {method!r}")
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"{method} {url} → HTTP {resp.status_code}: {resp.text[:200]!r}"
                )
            return resp.json()
    # urllib fallback
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator CLI, host is trusted
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} → connection failed: {e.reason}") from e


def _format_ms(value: float) -> str:
    """Right-aligned, 8-wide, 1 decimal place."""
    return f"{value:8.1f} ms"


def _format_pct(value: float) -> str:
    return f"{value:6.2f} %"


def _recommendation_for(
    endpoint: dict,
    *,
    p95_target_ms: float,
    p99_target_ms: float,
    error_rate_target_pct: float,
) -> list[str]:
    """Return actionable recommendations for an endpoint that exceeded a target."""
    recs: list[str] = []
    p95 = endpoint.get("p95_ms", 0) or 0
    p99 = endpoint.get("p99_ms", 0) or 0
    err = endpoint.get("error_rate", 0) or 0
    avg = endpoint.get("avg_latency_ms", 0) or 0
    method = endpoint.get("method", "GET")
    path = endpoint.get("endpoint", "?")

    if err >= error_rate_target_pct:
        recs.append(
            f"⚠️  error_rate={_format_pct(err)} exceeds target "
            f"{_format_pct(error_rate_target_pct)} — investigate upstream failures / "
            f"recent deploys for {method} {path}"
        )
    if p99 >= p99_target_ms:
        if avg < (p95 * 0.5):
            recs.append(
                f"📈 tail-latency spike: avg={_format_ms(avg)} but p99={_format_ms(p99)} "
                f"— look for occasional blocking I/O (DB lock, GC pause, downstream timeout)"
            )
        else:
            recs.append(
                f"📈 p99={_format_ms(p99)} exceeds target {_format_ms(p99_target_ms)} — "
                f"consider caching, batching N+1 queries, or moving work off the request path"
            )
    if p95 >= p95_target_ms and p99 < p99_target_ms:
        recs.append(
            f"🐌 p95={_format_ms(p95)} exceeds target {_format_ms(p95_target_ms)} (p99 OK) — "
            f"broad slowness, not a tail spike; apply cache / memoisation to the route handler"
        )
    return recs


def render_report(
    stats: dict,
    slowest: dict,
    *,
    limit: int,
    p95_target_ms: float,
    p99_target_ms: float,
    error_rate_target_pct: float,
) -> tuple[str, int]:
    """Render the human-readable report. Returns ``(text, exit_code)``."""
    lines: list[str] = []
    summary = stats.get("summary", {}) or {}
    endpoints = stats.get("endpoints", []) or []
    slowest_eps = slowest.get("slowest", []) or []

    lines.append("═" * 78)
    lines.append("  Polymarket-Bot — Performance Report  (W15-4)")
    lines.append("═" * 78)
    lines.append("")
    lines.append("  Summary")
    lines.append("  ────────")
    lines.append(f"  total endpoints : {summary.get('total_endpoints', 0)}")
    lines.append(f"  total requests  : {summary.get('total_requests', 0)}")
    lines.append(f"  total errors    : {summary.get('total_errors', 0)}")
    lines.append(f"  overall error % : {_format_pct(summary.get('overall_error_rate', 0))}")
    lines.append("")
    lines.append(f"  Targets: p95 ≤ {_format_ms(p95_target_ms)}  "
                 f"p99 ≤ {_format_ms(p99_target_ms)}  "
                 f"err ≤ {_format_pct(error_rate_target_pct)}")
    lines.append("")

    # ── Top slowest endpoints ────────────────────────────────────────────────
    lines.append("  Slowest endpoints (by p95 latency, descending)")
    lines.append("  " + "─" * 76)
    header = (
        f"  {'method':<6}  {'endpoint':<36}  {'count':>7}  "
        f"{'avg':>10}  {'p50':>10}  {'p95':>10}  {'p99':>10}  {'err%':>7}"
    )
    lines.append(header)
    lines.append("  " + "─" * 76)
    for ep in slowest_eps[:limit]:
        lines.append(_render_endpoint_row(ep))
    if not slowest_eps:
        lines.append("  (no endpoints recorded yet — wait for traffic)")
    lines.append("")

    # ── Full endpoint table (top N) ──────────────────────────────────────────
    lines.append(f"  All endpoints (top {limit}, sorted by p95)")
    lines.append("  " + "─" * 76)
    lines.append(header)
    lines.append("  " + "─" * 76)
    for ep in endpoints[:limit]:
        lines.append(_render_endpoint_row(ep))
    if not endpoints:
        lines.append("  (no endpoints recorded yet — wait for traffic)")
    lines.append("")

    # ── Recommendations ───────────────────────────────────────────────────────
    lines.append("  Recommendations")
    lines.append("  " + "─" * 76)
    flagged_count = 0
    for ep in endpoints:
        recs = _recommendation_for(
            ep,
            p95_target_ms=p95_target_ms,
            p99_target_ms=p99_target_ms,
            error_rate_target_pct=error_rate_target_pct,
        )
        if recs:
            flagged_count += 1
            lines.append(f"  • {ep.get('method', 'GET')} {ep.get('endpoint', '?')}")
            for r in recs:
                lines.append(f"      {r}")
    if flagged_count == 0:
        lines.append("  ✓ every endpoint is within target — no action required.")
    lines.append("")
    lines.append("═" * 78)
    if flagged_count > 0:
        lines.append(f"  {flagged_count} endpoint(s) flagged for action — see recommendations above.")
        exit_code = 1
    else:
        lines.append("  All endpoints within target — exit 0.")
        exit_code = 0
    lines.append("═" * 78)
    return "\n".join(lines), exit_code


def _render_endpoint_row(ep: dict) -> str:
    method = (ep.get("method") or "GET")[:6]
    path = (ep.get("endpoint") or "?")[:36]
    return (
        f"  {method:<6}  {path:<36}  {ep.get('request_count', 0):>7}  "
        f"{_format_ms(ep.get('avg_latency_ms', 0))}  "
        f"{_format_ms(ep.get('p50_ms', 0))}  "
        f"{_format_ms(ep.get('p95_ms', 0))}  "
        f"{_format_ms(ep.get('p99_ms', 0))}  "
        f"{_format_pct(ep.get('error_rate', 0))}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a performance report from the live polymarket-bot profiler.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Bot API base URL (default: {DEFAULT_URL}; env: BOT_API_URL)",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Bearer token (env: API_TOKEN or BOT_API_TOKEN)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of endpoints to list (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--p95-target-ms",
        type=float,
        default=DEFAULT_P95_TARGET_MS,
        help=f"p95 latency target in ms (default: {DEFAULT_P95_TARGET_MS})",
    )
    parser.add_argument(
        "--p99-target-ms",
        type=float,
        default=DEFAULT_P99_TARGET_MS,
        help=f"p99 latency target in ms (default: {DEFAULT_P99_TARGET_MS})",
    )
    parser.add_argument(
        "--error-rate-target-pct",
        type=float,
        default=DEFAULT_ERROR_RATE_TARGET_PCT,
        help=f"error rate target in %% (default: {DEFAULT_ERROR_RATE_TARGET_PCT})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Call POST /api/profiling/reset after reading the stats.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of the human-readable report (for piping to jq).",
    )
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    try:
        stats = _fetch_json(f"{base}/api/profiling/stats", args.token)
        slowest = _fetch_json(f"{base}/api/profiling/slowest?limit={args.limit}", args.token)
    except RuntimeError as e:
        print(f"[perf_report] FAILED to fetch stats: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"stats": stats, "slowest": slowest}, indent=2))
        # Exit-code semantics still apply for the recommendations.
        flagged = 0
        for ep in (stats.get("endpoints") or []):
            recs = _recommendation_for(
                ep,
                p95_target_ms=args.p95_target_ms,
                p99_target_ms=args.p99_target_ms,
                error_rate_target_pct=args.error_rate_target_pct,
            )
            if recs:
                flagged += 1
        return 1 if flagged > 0 else 0

    text, exit_code = render_report(
        stats,
        slowest,
        limit=args.limit,
        p95_target_ms=args.p95_target_ms,
        p99_target_ms=args.p99_target_ms,
        error_rate_target_pct=args.error_rate_target_pct,
    )
    print(text)

    if args.reset:
        try:
            _fetch_json(f"{base}/api/profiling/reset", args.token, method="POST")
            print("[perf_report] profiling data reset via POST /api/profiling/reset",
                  file=sys.stderr)
        except RuntimeError as e:
            print(f"[perf_report] reset FAILED: {e}", file=sys.stderr)
            # Don't change exit_code — the report itself already succeeded.

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
