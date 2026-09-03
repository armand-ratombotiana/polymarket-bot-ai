#!/usr/bin/env python3
"""Lightweight monitoring daemon for the Polymarket bot platform.

Runs ``scripts/health_check.py`` (imported in-process, no subprocess) every
``--interval`` seconds and:

  * Appends each sample to a rotating JSONL log (``--log``).
  * Emits an alert line to **stderr** whenever the overall pass/fail state
    *changes* (e.g. ``OK → DEGRADED``). Intermediate samples that don't
    change the state are logged silently.
  * Forwards ``--once`` to exit after a single sample (useful as a cron
    one-shot or as a smoke test from CI).

Designed to run as a long-lived background process under systemd
(``polymarket-monitor.service``) or directly:

    python scripts/monitor.py                    # foreground, 60s loop
    python scripts/monitor.py --interval 30 &    # 30s loop in background
    python scripts/monitor.py --once             # one-shot, exit 0/1

Exit codes:
  0  — daemon shut down cleanly (SIGTERM/SIGINT) or --once sample passed
  1  — --once sample failed, or fatal init error
  2  — invalid CLI arguments

No external dependencies. Uses only the stdlib so the daemon can boot
even before the project venv is fully populated.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow ``python scripts/monitor.py`` from repo root without an installed
# package — we just need the sibling health_check module.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Importing here (rather than inside the loop) lets import errors surface
# immediately on startup instead of after the first interval elapses.
import health_check  # noqa: E402  (path munged above)


DEFAULT_INTERVAL = 60
DEFAULT_LOG = Path(
    os.environ.get(
        "MONITOR_LOG",
        "/home/z/my-project/mini-services/polymarket-bot/data/health_monitor.jsonl",
    )
)

# Global flag flipped by SIGTERM/SIGINT handlers so the main loop can drain.
_STOP = False


def _handle_signal(signum, _frame):  # noqa: ANN001 — signal callback signature
    global _STOP
    _STOP = True
    # stderr so logs don't pollute the JSONL stream on stdout.
    print(f"[monitor] received signal {signum}, shutting down", file=sys.stderr)


def _run_one_sample() -> dict:
    """Invoke health_check's checks once and return the report dict."""
    # health_check.py is structured so its top-level statements run on import.
    # We instead call its ``main`` function, which returns the exit code and
    # prints the JSON report to stdout. To avoid double-printing under the
    # daemon, we temporarily silence stdout while main() runs, then parse
    # the JSON it emitted.
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        # Reset module-level state so we don't accumulate checks across runs.
        health_check.checks = []
        # Honour --quiet so health_check skips the human-readable progress lines.
        prev_quiet = health_check.QUIET
        health_check.QUIET = True
        try:
            exit_code = health_check.main()
        finally:
            health_check.QUIET = prev_quiet

    try:
        report = json.loads(buf.getvalue())
    except json.JSONDecodeError:
        # If health_check ever prints non-JSON to stdout, fall back to a
        # minimal report so the daemon keeps logging rather than crashing.
        report = {
            "checks": [],
            "passed": 0,
            "total": 0,
            "all_passed": False,
            "parse_error": buf.getvalue()[:500],
        }
    report["exit_code"] = exit_code
    return report


def _append_log(log_path: Path, record: dict) -> None:
    """Append one JSONL record, creating parent dirs as needed."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        print(f"[monitor] failed to write log: {exc}", file=sys.stderr)


def _classify(report: dict) -> str:
    """Map a health report to a coarse state label."""
    if report.get("all_passed"):
        return "OK"
    if report.get("passed", 0) == 0:
        return "DOWN"
    return "DEGRADED"


def run_forever(interval: int, log_path: Path, once: bool = False) -> int:
    """Main loop. Returns process exit code."""
    # Wire up signal handlers for graceful shutdown.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    prev_state: str | None = None

    while not _STOP:
        sample_start = time.time()
        report = _run_one_sample()
        state = _classify(report)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Decorate the log record with daemon-level metadata.
        record = {
            "ts": now_iso,
            "epoch": sample_start,
            "state": state,
            "passed": report.get("passed", 0),
            "total": report.get("total", 0),
            "all_passed": report.get("all_passed", False),
            "exit_code": report.get("exit_code"),
            "checks": report.get("checks", []),
        }
        _append_log(log_path, record)

        # State transition → emit alert line on stderr.
        if state != prev_state:
            if prev_state is None:
                # First sample: informational, not an alert.
                print(
                    f"[monitor] {now_iso} initial state = {state} "
                    f"({record['passed']}/{record['total']} checks passed)",
                    file=sys.stderr,
                )
            else:
                # Genuine transition: surface as an alert.
                print(
                    f"[monitor] ALERT {now_iso}: state changed "
                    f"{prev_state} → {state} "
                    f"({record['passed']}/{record['total']} checks passed)",
                    file=sys.stderr,
                )
            prev_state = state

        if once:
            return 0 if state == "OK" else 1

        # Sleep the remainder of the interval, but wake every second so
        # SIGTERM/SIGINT don't have to wait for the full interval to land.
        elapsed = time.time() - sample_start
        remaining = max(0.0, interval - elapsed)
        wake_step = 1.0
        slept = 0.0
        while slept < remaining and not _STOP:
            time.sleep(min(wake_step, remaining - slept))
            slept += wake_step

    print(f"[monitor] graceful shutdown at "
          f"{datetime.now(timezone.utc).isoformat()}", file=sys.stderr)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polymarket bot health monitoring daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between samples (default {DEFAULT_INTERVAL}).",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"JSONL log path (default {DEFAULT_LOG}).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single sample and exit (exit 0 if OK, 1 otherwise).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.interval <= 0:
        print("error: --interval must be a positive integer", file=sys.stderr)
        return 2
    try:
        return run_forever(args.interval, args.log, once=args.once)
    except KeyboardInterrupt:
        # Already handled by the SIGINT signal handler, but defensive.
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        print(f"[monitor] fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
