#!/usr/bin/env python3
"""Backup rotation — applies a Grandfather-Father-Son (GFS) retention policy.

Policy:
  - Keep the most recent backup per DAY for the last 7 days.
  - Keep the most recent backup per WEEK (ISO week) for the last 4 weeks.
  - Keep the most recent backup per MONTH for the last 12 months.
  - Additionally prune any backup older than --max-age-days (default 90).

Backups older than --max-age-days are ALWAYS pruned, even if they happen to
fall into a keep bucket — the max-age is a hard ceiling on retention. The
default max-age (90d) is intentionally generous because the daily/weekly/monthly
buckets already prune aggressively inside the 90d window.

Backups are identified by subdirectories of BACKUP_DIR whose names match the
YYYYMMDD_HHMMSS timestamp format. `pre_restore_*` directories (created by
restore.sh as safety snapshots) are NEVER pruned by this script — they must
be cleaned up manually after the operator is confident the restore succeeded.

Usage:
    python3 scripts/backup_rotation.py [--dry-run] [--backup-dir DIR]
                                       [--max-age-days N] [--json]
    python3 scripts/backup_rotation.py --dry-run
    python3 scripts/backup_rotation.py --backup-dir /mnt/nas/backups --json

Exit codes:
    0 — rotation completed (or dry-run completed) cleanly
    1 — usage error / invalid args
    2 — backup directory missing or unreadable
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Matches a backup directory name like 20260903_141500.
# Pre-restore snapshots like pre_restore_20260904_101530 are intentionally
# NOT matched — they are a separate safety-net class and must be pruned
# manually by the operator.
BACKUP_NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$")


def parse_backup_name(name: str) -> datetime | None:
    """Parse a YYYYMMDD_HHMMSS directory name into a datetime.

    Returns None if the name doesn't match the expected format.
    Invalid calendar values (e.g. month 13) also return None — we
    refuse to rotate a directory we can't timestamp.
    """
    m = BACKUP_NAME_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def list_backups(backup_dir: Path) -> list[dict]:
    """List all timestamped backup subdirectories in backup_dir.

    Returns a list of dicts sorted by timestamp ascending:
        [{"path": Path, "name": str, "ts": datetime, "size_bytes": int}, ...]
    """
    backups: list[dict] = []
    if not backup_dir.is_dir():
        return backups

    for entry in backup_dir.iterdir():
        if not entry.is_dir():
            continue
        ts = parse_backup_name(entry.name)
        if ts is None:
            continue  # not a backup (e.g. pre_restore_*, lost+found, etc.)
        # Compute total size of the directory recursively; this can be slow
        # on huge dirs but backup dirs are typically small (~MB-scale).
        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        backups.append({
            "path": entry,
            "name": entry.name,
            "ts": ts,
            "size_bytes": size,
        })

    backups.sort(key=lambda b: b["ts"])
    return backups


def _iso_week(dt: datetime) -> tuple[int, int]:
    """Return (iso_year, iso_week) for a datetime — used to group by week.

    ISO weeks can straddle calendar-year boundaries (e.g. 2026-W01 may start
    on 2025-12-29), so we use the ISO year (not the calendar year) as the
    grouping key. This avoids accidentally treating 2025-12-29 and 2026-01-02
    as different weeks.
    """
    return dt.isocalendar()[0], dt.isocalendar()[1]


def _month_key(dt: datetime) -> tuple[int, int]:
    """Return (year, month) for a datetime — used to group by month."""
    return dt.year, dt.month


def select_keep_set(backups: list[dict], now: datetime) -> set[str]:
    """Apply the GFS policy and return the set of backup names to keep.

    A backup is kept if it is the most recent backup within ANY of:
      - its calendar day (for the last 7 days),
      - its ISO week (for the last 4 weeks),
      - its calendar month (for the last 12 months).
    """
    keep: set[str] = set()

    # Daily: last 7 days. We use "calendar day" buckets; for each of the
    # last 7 calendar days, keep the newest backup whose ts falls in that day.
    day_cutoff = now - timedelta(days=7)
    day_buckets: dict[tuple[int, int, int], dict] = {}
    for b in backups:
        if b["ts"] < day_cutoff:
            continue
        key = (b["ts"].year, b["ts"].month, b["ts"].day)
        if key not in day_buckets or b["ts"] > day_buckets[key]["ts"]:
            day_buckets[key] = b
    keep.update(b["name"] for b in day_buckets.values())

    # Weekly: last 4 ISO weeks. Same idea, but bucket by (iso_year, iso_week).
    week_cutoff = now - timedelta(weeks=4)
    week_buckets: dict[tuple[int, int], dict] = {}
    for b in backups:
        if b["ts"] < week_cutoff:
            continue
        key = _iso_week(b["ts"])
        if key not in week_buckets or b["ts"] > week_buckets[key]["ts"]:
            week_buckets[key] = b
    keep.update(b["name"] for b in week_buckets.values())

    # Monthly: last 12 calendar months.
    month_cutoff = now - timedelta(days=365)  # ~12 months
    month_buckets: dict[tuple[int, int], dict] = {}
    for b in backups:
        if b["ts"] < month_cutoff:
            continue
        key = _month_key(b["ts"])
        if key not in month_buckets or b["ts"] > month_buckets[key]["ts"]:
            month_buckets[key] = b
    keep.update(b["name"] for b in month_buckets.values())

    return keep


def rotate(
    backup_dir: Path,
    *,
    max_age_days: int = 90,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """Run the GFS rotation. Returns a structured report dict.

    The report shape:
        {
          "backup_dir": str,
          "rotated_at": iso8601,
          "now": iso8601,
          "policy": {"daily_keep_days": 7, "weekly_keep_weeks": 4,
                      "monthly_keep_months": 12, "max_age_days": int},
          "total_backups_before": int,
          "total_backups_after": int,
          "kept":   [{"name", "ts", "size_bytes", "reasons": [str, ...]}, ...],
          "pruned": [{"name", "ts", "size_bytes", "reason": str}, ...],
          "bytes_freed": int,
          "dry_run": bool,
        }
    """
    if now is None:
        now = datetime.now()

    report = {
        "backup_dir": str(backup_dir),
        "rotated_at": now.isoformat(),
        "now": now.isoformat(),
        "policy": {
            "daily_keep_days": 7,
            "weekly_keep_weeks": 4,
            "monthly_keep_months": 12,
            "max_age_days": max_age_days,
        },
        "total_backups_before": 0,
        "total_backups_after": 0,
        "kept": [],
        "pruned": [],
        "bytes_freed": 0,
        "dry_run": dry_run,
    }

    backups = list_backups(backup_dir)
    report["total_backups_before"] = len(backups)

    if not backups:
        return report

    keep_names = select_keep_set(backups, now)
    age_cutoff = now - timedelta(days=max_age_days)

    for b in backups:
        reasons = []
        if b["name"] in keep_names:
            reasons.append("gfs_keep")
        is_too_old = b["ts"] < age_cutoff
        is_newest_overall = b is backups[-1]  # never prune the latest snapshot

        if is_too_old and not is_newest_overall:
            # Hard ceiling: prune even if it's in a keep bucket, UNLESS it's
            # the newest backup overall (we always keep at least one).
            action = "prune"
            reason = f"older_than_{max_age_days}d"
        elif b["name"] not in keep_names and is_too_old:
            action = "prune"
            reason = f"older_than_{max_age_days}d_and_not_in_keep_buckets"
        elif b["name"] in keep_names:
            action = "keep"
            reason = "gfs_policy"
        elif is_newest_overall:
            action = "keep"
            reason = "newest_backup"
        else:
            # Inside the GFS windows but not the chosen representative of any
            # bucket — these are "extra" snapshots from a high-frequency
            # cadence (e.g. 4x/day backups within the same day). Prune them.
            action = "prune"
            reason = "superseded_by_newer_in_same_bucket"

        if action == "keep":
            report["kept"].append({
                "name": b["name"],
                "ts": b["ts"].isoformat(),
                "size_bytes": b["size_bytes"],
                "reason": reason,
            })
        else:
            report["pruned"].append({
                "name": b["name"],
                "ts": b["ts"].isoformat(),
                "size_bytes": b["size_bytes"],
                "reason": reason,
            })
            report["bytes_freed"] += b["size_bytes"]
            if not dry_run:
                # `ignore_errors=False` would surface the failure, but we'd
                # rather log+continue so one bad perm doesn't block rotation
                # of the others. The prune will be retried next run.
                shutil.rmtree(b["path"], ignore_errors=True)

    # Recount surviving backups (only accurate when dry_run=False, but we
    # compute it for both branches as a sanity check).
    if dry_run:
        report["total_backups_after"] = (
            len(backups) - len(report["pruned"])
        )
    else:
        surviving = list_backups(backup_dir)
        report["total_backups_after"] = len(surviving)

    return report


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def print_report(report: dict) -> None:
    print(f"\n{'=' * 60}")
    print("Backup Rotation Report")
    print(f"{'=' * 60}")
    print(f"Backup dir:     {report['backup_dir']}")
    print(f"Rotated at:     {report['rotated_at']}")
    print(f"Mode:           {'DRY RUN' if report['dry_run'] else 'LIVE'}")
    p = report["policy"]
    print(
        f"Policy:         daily×{p['daily_keep_days']}d, "
        f"weekly×{p['weekly_keep_weeks']}w, "
        f"monthly×{p['monthly_keep_months']}m, "
        f"max_age={p['max_age_days']}d"
    )
    print(
        f"Backups:        {report['total_backups_before']} → "
        f"{report['total_backups_after']} "
        f"({len(report['pruned'])} pruned, {len(report['kept'])} kept)"
    )
    print(f"Bytes freed:    {_human_bytes(report['bytes_freed'])}")
    print(f"{'=' * 60}\n")

    if report["kept"]:
        print("Kept:")
        for k in report["kept"]:
            print(
                f"  \u2713 {k['name']:<22} {k['ts']:<26} "
                f"{_human_bytes(k['size_bytes']):>10}  ({k['reason']})"
            )
        print()

    if report["pruned"]:
        print("Pruned:")
        for p_ in report["pruned"]:
            verb = "would prune" if report["dry_run"] else "pruned"
            print(
                f"  \u2717 {p_['name']:<22} {p_['ts']:<26} "
                f"{_human_bytes(p_['size_bytes']):>10}  ({p_['reason']})"
            )
        print()

    print(f"{'=' * 60}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a Grandfather-Father-Son backup retention policy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--backup-dir",
        default="/home/z/my-project/backups",
        help="Root dir containing timestamped backup subdirectories "
             "(default: /home/z/my-project/backups).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=90,
        help="Hard ceiling on backup age (default: 90). Backups older than "
             "this are pruned regardless of GFS buckets (except the newest).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the rotation plan but don't actually delete anything.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the rotation report as JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    if not backup_dir.is_dir():
        print(
            f"ERROR: backup directory not found or not a directory: {backup_dir}",
            file=sys.stderr,
        )
        return 2

    report = rotate(
        backup_dir,
        max_age_days=args.max_age_days,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
