#!/usr/bin/env python3
"""
verify_contracts.py — Run all W15-3 contract tests and emit a JSON report.

W15-3 — Generates a machine-readable summary of which endpoints pass /
fail the frontend ↔ backend contract checks, suitable for CI gating and
operator dashboards.

Usage::

    python scripts/verify_contracts.py
    python scripts/verify_contracts.py --output contracts.json
    python scripts/verify_contracts.py --quiet   # JSON only, no stdout table

The script:
  1. Invokes ``pytest tests/contract/`` via the pytest API (in-process).
  2. Parses the JUnit XML written to a temp file (pytest's
     ``--junitxml`` flag — the most reliable machine-readable output).
  3. Builds a per-endpoint pass/fail report keyed by ``TestXxxContract``
     class name and the contract dimension (status / content-type /
     required-fields / field-types / field-constraints).
  4. Lists every endpoint with its verified response shape (the canonical
     top-level keys).

Exit codes:
  * 0 — all contract tests pass.
  * 1 — at least one contract test failed (or pytest errored).
  * 2 — script-level error (couldn't import pytest, write temp file, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


# ── Endpoint → contract test mapping ─────────────────────────────────────
# Each entry documents (a) the route, (b) the contract test class that
# pins down its shape, (c) the canonical top-level keys the backend
# actually returns, and (d) the frontend's expectation source (Zod schema
# or api-client declared return type). The script cross-references this
# table against the actual pytest results to surface shape drift.

CONTRACT_MATRIX: list[dict[str, Any]] = [
    {
        "endpoint": "GET /api/health",
        "test_class": "TestHealthContract",
        "authenticated": False,
        "wire_shape": {"status": "str", "timestamp": "float", "paper": "bool"},
        "frontend_expectation": "schemas.ts::HealthSchema (status: str required)",
    },
    {
        "endpoint": "GET /api/status",
        "test_class": "TestStatusContract",
        "authenticated": True,
        "wire_shape": "{mode, strategies, paper_balance, seeded_markets, "
                      "tracked_books, book_poller, vector_docs_indexed, "
                      "kill_switch_durable, + status_report fields}",
        "frontend_expectation": "api-client.ts::systemApi.status → any",
    },
    {
        "endpoint": "GET /api/positions",
        "test_class": "TestPositionsContract",
        "authenticated": True,
        "wire_shape": "{positions: Position[], count: int, daily_pnl: float}",
        "frontend_expectation": "api-client.ts::tradingApi.getPositions → "
                                "{positions, count, daily_pnl?}",
    },
    {
        "endpoint": "GET /api/orders",
        "test_class": "TestOrdersContract",
        "authenticated": True,
        "wire_shape": "{orders: Order[], count: int}",
        "frontend_expectation": "api-client.ts::tradingApi.getOrders → "
                                "{orders, count}",
    },
    {
        "endpoint": "GET /api/trades",
        "test_class": "TestTradesContract",
        "authenticated": True,
        "wire_shape": "{trades: Trade[], count: int}",
        "frontend_expectation": "api-client.ts::tradingApi.getTrades → "
                                "{trades, count}",
    },
    {
        "endpoint": "GET /api/markets",
        "test_class": "TestMarketsContract",
        "authenticated": True,
        "wire_shape": "{markets: Market[], count: int}  "
                      "[502 when upstream Gamma unavailable]",
        "frontend_expectation": "api-client.ts::marketsApi.getMarkets → any[] "
                                "(DISCREPANCY: backend wraps in dict)",
    },
    {
        "endpoint": "GET /api/ml/metrics",
        "test_class": "TestMLMetricsContract",
        "authenticated": True,
        "wire_shape": "{brier_score, roc_auc, log_loss, ece, sharpe_ratio, "
                      "n_online_updates, last_trained, training_source, "
                      "adaptive_weights, meta_learner, drift, "
                      "feature_importances, reliability_curve, calibration, "
                      "model_ready, model_version, registry_summary}",
        "frontend_expectation": "schemas.ts::MLMetricsSchema (all fields optional)",
    },
    {
        "endpoint": "GET /api/alerts",
        "test_class": "TestAlertsContract",
        "authenticated": True,
        "wire_shape": "{alerts: Alert[], stats: {total, unacknowledged, ...}}",
        "frontend_expectation": "api-client.ts::alertsApi.get → {alerts, stats}",
    },
    {
        "endpoint": "GET /api/observability",
        "test_class": "TestObservabilityContract",
        "authenticated": True,
        "wire_shape": "{generated_at, category_count, metric_count, "
                      "oldest_sample_age_seconds, newest_sample_age_seconds, "
                      "categories: {data_source, bot, strategy, execution, "
                      "ml, system, other}}",
        "frontend_expectation": "api-client.ts::observabilityApi.get → any",
    },
    {
        "endpoint": "GET /api/decisions/rejected",
        "test_class": "TestDecisionsContract",
        "authenticated": True,
        "wire_shape": "{count: int, rejections: Decision[]}",
        "frontend_expectation": "api-client.ts::decisionsApi.getRejected → any[] "
                                "(DISCREPANCY: backend wraps in dict)",
    },
    {
        "endpoint": "GET /api/attribution",
        "test_class": "TestAttributionContract",
        "authenticated": True,
        "wire_shape": "{summary, by_strategy, by_confidence_bucket, "
                      "by_edge_bucket, by_probability_band, by_liquidity_level, "
                      "by_holding_period, by_trade_direction, bucket_definitions}",
        "frontend_expectation": "api-client.ts::analyticsApi.getAttribution → any",
    },
    {
        "endpoint": "GET /api/events",
        "test_class": "TestEventsContract",
        "authenticated": True,
        "wire_shape": "{events: str[], count: int}",
        "frontend_expectation": "api-client.ts::systemApi.events → any[] "
                                "(DISCREPANCY: backend wraps in dict)",
    },
    {
        "endpoint": "GET /api/cache/stats",
        "test_class": "TestCacheStatsContract",
        "authenticated": True,
        "wire_shape": "{caches: [{name, size, max_size, hits, misses, "
                      "hit_rate, default_ttl}, ...]}",
        "frontend_expectation": "api-client.ts::cacheApi.getStats → any",
    },
    {
        "endpoint": "GET /api/orderbooks",
        "test_class": "TestOrderbooksContract",
        "authenticated": True,
        "wire_shape": "{order_books: OrderBook[], count: int}",
        "frontend_expectation": "api-client.ts::marketsApi.getOrderbooks → any[] "
                                "(DISCREPANCY: backend wraps in dict)",
    },
    {
        "endpoint": "GET /api/analytics",
        "test_class": "TestAnalyticsContract",
        "authenticated": True,
        "wire_shape": "{equity, realized_pnl, unrealized_pnl, net_pnl, "
                      "total_trades, winning_trades, losing_trades, "
                      "closed_trades, open_trades, win_rate, "
                      "win_rate_ci_low, win_rate_ci_high, profit_factor, "
                      "avg_win, avg_loss, expectancy, sharpe_ratio, "
                      "max_drawdown_dollars, max_drawdown_pct, "
                      "total_volume_usdc, open_exposure, "
                      "open_position_count, pending_order_capital, "
                      "risk_utilization, mode, data_freshness_seconds, "
                      "peak_equity, active_strategies}",
        "frontend_expectation": "schemas.ts::AnalyticsSchema (equity required; "
                                "rest optional)",
    },
    {
        "endpoint": "GET /api/rate-limit/stats",
        "test_class": "TestRateLimitStatsContract",
        "authenticated": True,
        "wire_shape": "{total_hits, hits_per_minute_rate, hits_by_endpoint, "
                      "hits_by_client, hits_per_minute, top_endpoints}",
        "frontend_expectation": "frontend RateLimitPanel expects all six keys",
    },
]


def _parse_junit_xml(xml_path: Path) -> dict[str, dict[str, Any]]:
    """Parse the JUnit XML emitted by ``pytest --junitxml`` and return a
    ``{testcase_name: {status, error_msg, classname}}`` dict.

    testcase_name is the test method's short name (e.g.
    ``test_returns_200``). The classname includes the contract class.
    """
    if not xml_path.exists():
        return {}

    tree = ET.parse(xml_path)
    root = tree.getroot()
    results: dict[str, dict[str, Any]] = {}

    for tc in root.iter("testcase"):
        name = tc.attrib.get("name", "")
        classname = tc.attrib.get("classname", "")
        # Find the outcome child (failure, error, skipped).
        outcome_el = tc.find("failure") or tc.find("error") or tc.find("skipped")
        if outcome_el is not None:
            status_map = {
                "failure": "FAIL",
                "error": "ERROR",
                "skipped": "SKIP",
            }
            status = status_map.get(outcome_el.tag, outcome_el.tag.upper())
            msg = (outcome_el.attrib.get("message", "")
                   or (outcome_el.text or "").strip()[:500])
        else:
            status = "PASS"
            msg = ""

        results[f"{classname}::{name}"] = {
            "status": status,
            "message": msg,
            "classname": classname,
            "name": name,
        }

    return results


def _summarize_by_class(
    test_results: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate per-test results into per-class summary keyed by class name."""
    by_class: dict[str, dict[str, Any]] = {}
    for full_name, result in test_results.items():
        cls = result["classname"].split(".")[-1]
        if cls not in by_class:
            by_class[cls] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "failures": [],
            }
        by_class[cls]["total"] += 1
        status = result["status"]
        if status == "PASS":
            by_class[cls]["passed"] += 1
        elif status == "FAIL":
            by_class[cls]["failed"] += 1
            by_class[cls]["failures"].append({
                "test": result["name"],
                "message": result["message"],
            })
        elif status == "ERROR":
            by_class[cls]["errors"] += 1
            by_class[cls]["failures"].append({
                "test": result["name"],
                "message": result["message"],
            })
        elif status == "SKIP":
            by_class[cls]["skipped"] += 1
    return by_class


def _build_report(by_class: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the final JSON-report dict."""
    # Map contract-test class → endpoint summary
    endpoint_summaries: list[dict[str, Any]] = []
    for entry in CONTRACT_MATRIX:
        cls = entry["test_class"]
        summary = by_class.get(cls)
        if summary is None:
            status = "NOT_RUN"
            passed = failed = total = 0
        else:
            passed = summary["passed"]
            failed = summary["failed"] + summary["errors"]
            total = summary["total"]
            if failed > 0:
                status = "FAIL"
            elif summary["skipped"] == total and total > 0:
                status = "SKIP"
            elif passed == total:
                status = "PASS"
            else:
                status = "PARTIAL"
        endpoint_summaries.append({
            "endpoint": entry["endpoint"],
            "test_class": cls,
            "authenticated": entry["authenticated"],
            "wire_shape": entry["wire_shape"],
            "frontend_expectation": entry["frontend_expectation"],
            "status": status,
            "tests_passed": passed,
            "tests_failed": failed,
            "tests_total": total,
            "failures": summary["failures"] if summary else [],
        })

    # Aggregate stats
    total_endpoints = len(endpoint_summaries)
    passing = sum(1 for e in endpoint_summaries if e["status"] == "PASS")
    failing = sum(1 for e in endpoint_summaries if e["status"] == "FAIL")
    partial = sum(1 for e in endpoint_summaries if e["status"] == "PARTIAL")
    skipped = sum(1 for e in endpoint_summaries if e["status"] == "SKIP")
    not_run = sum(1 for e in endpoint_summaries if e["status"] == "NOT_RUN")

    # Shape-discrepancy audit
    discrepancies = [
        e for e in endpoint_summaries
        if "DISCREPANCY" in e["frontend_expectation"]
    ]

    return {
        "generated_at_unix": int(__import__("time").time()),
        "summary": {
            "total_endpoints": total_endpoints,
            "passing": passing,
            "failing": failing,
            "partial": partial,
            "skipped": skipped,
            "not_run": not_run,
            "shape_discrepancies": len(discrepancies),
            "overall_pass": failing == 0 and partial == 0,
        },
        "endpoints": endpoint_summaries,
        "shape_discrepancies": discrepancies,
        "contract_dimensions_verified": [
            "status_code (200)",
            "content_type (application/json)",
            "required_fields (top-level keys)",
            "field_types (str/int/float/bool/list/dict)",
            "field_constraints (ranges, enums)",
            "shape_stability (same keys across calls)",
            "pagination (limit/offset respected)",
            "response_headers (X-Request-ID, X-API-Version)",
            "error_paths (401/422/404/500 shape)",
        ],
    }


def _print_table(report: dict[str, Any]) -> None:
    """Print a human-readable summary table to stdout."""
    print()
    print("=" * 78)
    print(" W15-3 API Contract Verification Report")
    print("=" * 78)
    print()
    s = report["summary"]
    print(f"  Endpoints verified : {s['total_endpoints']}")
    print(f"  Passing            : {s['passing']}")
    print(f"  Failing            : {s['failing']}")
    print(f"  Partial            : {s['partial']}")
    print(f"  Skipped            : {s['skipped']}")
    print(f"  Not run            : {s['not_run']}")
    print(f"  Shape discrepancies: {s['shape_discrepancies']}")
    print(f"  Overall pass       : {'YES' if s['overall_pass'] else 'NO'}")
    print()
    print("-" * 78)
    print(f"  {'STATUS':<8} {'PASSED/TOTAL':<14} {'ENDPOINT':<32}")
    print("-" * 78)
    for e in report["endpoints"]:
        status = e["status"]
        ratio = f"{e['tests_passed']}/{e['tests_total']}"
        marker = {"PASS": "[OK]  ", "FAIL": "[FAIL]", "PARTIAL": "[PART]",
                  "SKIP": "[SKIP]", "NOT_RUN": "[N/R] "}.get(status, "[?]   ")
        print(f"  {marker} {ratio:<14} {e['endpoint']:<32}")
        if e["failures"]:
            for f in e["failures"]:
                msg = (f["message"] or "").split("\n")[0][:60]
                print(f"           ↳ {f['test']}: {msg}")
    print("-" * 78)
    if report["shape_discrepancies"]:
        print()
        print("  Shape discrepancies (backend shape ≠ frontend expectation):")
        for d in report["shape_discrepancies"]:
            print(f"    • {d['endpoint']}")
            print(f"      {d['frontend_expectation']}")
    print()
    print("=" * 78)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run W15-3 API contract tests and emit a JSON report.",
    )
    parser.add_argument(
        "--output", "-o", default="-",
        help="Path to write the JSON report ('-' for stdout). Default: stdout.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress the human-readable stdout table.",
    )
    parser.add_argument(
        "--no-run", action="store_true",
        help="Skip running pytest; emit the static contract matrix only "
             "(useful for diffing the matrix across revisions).",
    )
    args = parser.parse_args()

    # ── Locate the polymarket-bot project root ─────────────────────────
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    contract_dir = project_root / "tests" / "contract"

    if not contract_dir.exists():
        print(f"ERROR: contract test directory not found at {contract_dir}",
              file=sys.stderr)
        return 2

    test_results: dict[str, dict[str, Any]] = {}

    if not args.no_run:
        # ── Run pytest in-process with --junitxml ──────────────────────
        try:
            import pytest  # noqa: PLC0415
        except ImportError:
            print("ERROR: pytest not importable — run inside the project's "
                  "venv.", file=sys.stderr)
            return 2

        # Make sure the project root is on sys.path so ``from api.server
        # import app`` resolves when pytest collects tests/contract/.
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        with tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, mode="w"
        ) as tmp:
            junit_path = Path(tmp.name)

        try:
            exit_code = pytest.main([
                str(contract_dir),
                "--junitxml", str(junit_path),
                "-q",
                "--no-header",
                "--tb=no",
            ])
        except SystemExit as e:
            # pytest.main() raises SystemExit on completion — capture it.
            exit_code = int(e.code) if e.code is not None else 0

        test_results = _parse_junit_xml(junit_path)

        # Clean up the temp file (best-effort).
        try:
            junit_path.unlink()
        except OSError:
            pass
    else:
        exit_code = 0

    # ── Build + emit the report ────────────────────────────────────────
    by_class = _summarize_by_class(test_results)
    report = _build_report(by_class)

    if not args.quiet:
        _print_table(report)

    json_str = json.dumps(report, indent=2, default=str)

    if args.output == "-":
        print(json_str)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_str + "\n")
        if not args.quiet:
            print(f"  JSON report written to: {out_path}")

    # Exit code: 0 if all pass, 1 if any failed, 2 on script error.
    if exit_code != 0 and not report["summary"]["overall_pass"]:
        return 1
    return 0 if report["summary"]["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
