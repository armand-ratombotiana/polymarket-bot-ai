#!/usr/bin/env python3
"""Comprehensive health check for the Polymarket bot platform.

Checks:
1. Frontend (Next.js) is responding
2. Backend (FastAPI) is responding
3. Backend health endpoint returns healthy status
4. Database files exist and are accessible
5. Disk space is adequate
6. Memory usage is acceptable
7. ML model is loaded
8. Kill switch is not active (unless intended)
9. No recent critical alerts
10. Observability collector is running

Exits 0 if all checks pass, 1 if any fail.
Outputs JSON report to stdout.

Configuration via environment variables:
  FRONTEND_URL     default http://localhost:3000
  BACKEND_URL      default http://localhost:8080
  BOT_DATA_DIR     default /home/z/my-project/mini-services/polymarket-bot/data
  API_TOKEN        default is the dev token baked into the .env

Usage:
  python scripts/health_check.py            # human-readable + JSON
  python scripts/health_check.py --quiet   # JSON only (no progress lines)
  python scripts/health_check.py --json     # alias for --quiet
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080")
DATA_DIR = Path(
    os.environ.get(
        "BOT_DATA_DIR",
        "/home/z/my-project/mini-services/polymarket-bot/data",
    )
)
API_TOKEN = os.environ.get(
    "API_TOKEN",
    "I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT",
)

QUIET = "--quiet" in sys.argv or "--json" in sys.argv

checks: list[dict] = []


def add_check(name: str, passed: bool, details: str = "") -> None:
    """Record a single check result and emit a progress line (unless --quiet)."""
    checks.append(
        {
            "name": name,
            "passed": bool(passed),
            "details": details,
            "timestamp": time.time(),
        }
    )
    if not QUIET:
        status = "✓" if passed else "✗"
        print(f"  {status} {name}: {details}")


def http_get(url: str, timeout: float = 5.0):
    """GET with auth header; return (status_code, json_or_text) or (None, error)."""
    try:
        req = Request(url, headers={"Authorization": f"Bearer {API_TOKEN}"})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except (URLError, HTTPError, TimeoutError, ConnectionError, OSError) as e:
        return None, str(e)


def main() -> int:
    if not QUIET:
        print("=== Polymarket Bot Health Check ===")
        print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Frontend: {FRONTEND_URL}")
        print(f"Backend:  {BACKEND_URL}")
        print(f"Data dir: {DATA_DIR}")
        print()

    # 1. Frontend responding
    code, _ = http_get(FRONTEND_URL, timeout=5)
    add_check("frontend_responding", code is not None and code == 200, f"HTTP {code}")

    # 2. Backend responding (liveness probe)
    code, _ = http_get(f"{BACKEND_URL}/api/health", timeout=5)
    add_check("backend_responding", code is not None and code == 200, f"HTTP {code}")

    # 3. Backend health status — server.py returns {"status": "ok", ...}
    #    (PUBLIC_PATHS lets /api/health through without auth).
    if code == 200:
        _, health = http_get(f"{BACKEND_URL}/api/health", timeout=5)
        if isinstance(health, dict):
            is_healthy = health.get("status") == "ok" or health.get("healthy") is True
            add_check("backend_healthy", is_healthy, str(health)[:100])
        else:
            add_check("backend_healthy", False, "Invalid health response")
    else:
        add_check("backend_healthy", False, f"HTTP {code}")

    # 4. Databases — audit_trail.db, decision_ledger.db, observability.db, market.db
    for db_name in ["audit_trail.db", "decision_ledger.db", "observability.db", "market.db"]:
        db_path = DATA_DIR / db_name
        try:
            exists = db_path.exists()
            size_mb = db_path.stat().st_size / 1024 / 1024 if exists else 0
            # Probe that the SQLite header is readable (not corrupted / locked
            # exclusively by another process). A bare SELECT 1 is enough; we
            # don't import sqlite3 at module scope so a missing stdlib on exotic
            # runtimes doesn't kill the whole health check.
            if exists:
                import sqlite3  # lazy import — see comment above

                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    conn.execute("SELECT 1").fetchone()
                    accessible = True
                except sqlite3.DatabaseError as exc:
                    accessible = False
                    add_check(
                        f"db_{db_name}",
                        False,
                        f"{size_mb:.1f}MB but unreadable: {exc}",
                    )
                finally:
                    conn.close()
                if accessible:
                    add_check(f"db_{db_name}", True, f"{size_mb:.1f}MB, readable")
            else:
                add_check(f"db_{db_name}", False, "MISSING")
        except Exception as exc:  # noqa: BLE001 — best-effort, never crash the script
            add_check(f"db_{db_name}", False, f"error: {exc}")

    # 5. Disk space — need at least 1 GB free on the data volume
    try:
        usage = os.statvfs(str(DATA_DIR))
        free_gb = (usage.f_bavail * usage.f_frsize) / 1024 / 1024 / 1024
        add_check("disk_space", free_gb > 1.0, f"{free_gb:.1f}GB free")
    except Exception as exc:  # noqa: BLE001
        add_check("disk_space", False, str(exc))

    # 6. Memory — keep headroom under 90 % used
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                if ":" in line:
                    key, _, val = line.partition(":")
                    parts = val.strip().split()
                    if parts:
                        mem[key] = parts[0]
        total = int(mem.get("MemTotal", "0"))
        available = int(mem.get("MemAvailable", "0"))
        used_pct = ((total - available) / total * 100) if total > 0 else 100
        add_check("memory_ok", used_pct < 90, f"{used_pct:.0f}% used")
    except Exception as exc:  # noqa: BLE001
        # /proc/meminfo doesn't exist on macOS — report skipped rather than fail.
        add_check("memory_ok", False, f"unavailable: {exc}")

    # 7. ML model loaded — /api/ml returns model_ready / model_version
    code, ml_data = http_get(f"{BACKEND_URL}/api/ml", timeout=10)
    if code == 200 and isinstance(ml_data, dict):
        has_model = bool(
            ml_data.get("model_ready")
            or ml_data.get("model_version")
            or ml_data.get("roc_auc")
            or ml_data.get("brier_score") is not None
        )
        add_check("ml_model_loaded", has_model, str(ml_data)[:80])
    else:
        add_check("ml_model_loaded", False, f"HTTP {code}")

    # 8. Kill switch status — /api/status returns kill_switch + kill_switch_durable
    code, status = http_get(f"{BACKEND_URL}/api/status", timeout=5)
    if code == 200 and isinstance(status, dict):
        kill_active = bool(
            status.get("kill_switch_active")
            or status.get("kill_switch")
            or status.get("kill_switch_durable")
        )
        # Pass always — we're surfacing the flag, not gating on it (operator may
        # have intentionally activated it).
        add_check("kill_switch_status", True, f"active={kill_active}")
    else:
        add_check("kill_switch_status", False, f"HTTP {code}")

    # 9. Recent alerts — /api/alerts returns {alerts: [...], stats: {...}}
    code, alerts_data = http_get(f"{BACKEND_URL}/api/alerts?limit=5", timeout=5)
    if code == 200 and isinstance(alerts_data, dict):
        alerts = alerts_data.get("alerts", [])
        critical = [
            a
            for a in alerts
            if a.get("severity") == "critical" and not a.get("acknowledged")
        ]
        add_check(
            "no_critical_alerts",
            len(critical) == 0,
            f"{len(critical)} unacknowledged critical",
        )
    else:
        add_check("no_critical_alerts", False, f"HTTP {code}")

    # 10. Observability collector — /api/observability returns the structured
    #     health report; a non-zero metric_count means the collector has
    #     recorded at least one sample since startup.
    code, obs_data = http_get(f"{BACKEND_URL}/api/observability", timeout=5)
    if code == 200 and isinstance(obs_data, dict):
        # The structured report shape is {categories: {<cat>: {<name>: {...}}}}
        # so count metrics across every category bucket.
        categories = obs_data.get("categories", {})
        if isinstance(categories, dict):
            metric_count = sum(
                len(v) for v in categories.values() if isinstance(v, dict)
            )
        else:
            metric_count = int(obs_data.get("metric_count", 0))
        add_check("observability_active", metric_count > 0, f"{metric_count} metrics")
    else:
        add_check("observability_active", False, f"HTTP {code}")

    # ── Summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    all_passed = passed == total

    if not QUIET:
        print()
        print(f"=== Summary: {passed}/{total} checks passed ===")

    report = {
        "checks": checks,
        "passed": passed,
        "total": total,
        "all_passed": all_passed,
        "timestamp": time.time(),
        "frontend_url": FRONTEND_URL,
        "backend_url": BACKEND_URL,
        "data_dir": str(DATA_DIR),
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
