#!/usr/bin/env python3
"""Verify backup integrity — checks that backup files are valid SQLite databases
with all expected tables and row counts.

Usage:
    python scripts/verify_backup.py <backup_dir>
    python scripts/verify_backup.py backups/20250101_120000
"""
import sys
import os
import sqlite3
import json
import gzip
import tempfile
import hashlib
from pathlib import Path
from datetime import datetime

# Expected tables per database
EXPECTED_TABLES = {
    "audit_trail.db": ["audit_events"],
    "decision_ledger.db": ["decisions"],
    "execution_quality.db": ["execution_quality"],
    "observability.db": ["observability_metrics"],
    "closed_positions.db": ["closed_positions"],
    "alerts.db": ["alerts"],
    "feature_flags.db": ["feature_flags"],
    "ab_tests.db": ["experiments", "predictions"],
    "market.db": ["order_books", "market_snapshots"],
}

def verify_database(db_path: Path, expected_tables: list[str]) -> dict:
    """Verify a single SQLite database."""
    result = {
        "file": db_path.name,
        "size_bytes": db_path.stat().st_size,
        "valid": True,
        "errors": [],
        "tables": {},
    }

    try:
        with sqlite3.connect(str(db_path)) as conn:
            # Integrity check
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                result["valid"] = False
                result["errors"].append(f"Integrity check failed: {integrity}")

            # Check expected tables
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            actual_tables = {row[0] for row in cursor.fetchall()}

            for expected in expected_tables:
                if expected not in actual_tables:
                    result["errors"].append(f"Missing table: {expected}")
                    result["valid"] = False
                else:
                    count = conn.execute(f"SELECT COUNT(*) FROM {expected}").fetchone()[0]
                    result["tables"][expected] = count

            result["actual_tables"] = list(actual_tables)

    except Exception as e:
        result["valid"] = False
        result["errors"].append(str(e))

    return result

def verify_backup_dir(backup_dir: Path) -> dict:
    """Verify all databases in a backup directory."""
    if not backup_dir.exists():
        return {"error": f"Backup directory not found: {backup_dir}"}

    results = {
        "backup_dir": str(backup_dir),
        "verified_at": datetime.now().isoformat(),
        "databases": [],
        "all_valid": True,
    }

    # Check for MANIFEST.txt
    manifest = backup_dir / "MANIFEST.txt"
    if not manifest.exists():
        results["warnings"] = ["MANIFEST.txt not found"]

    # Verify each .db file (may be gzipped)
    for db_name, expected_tables in EXPECTED_TABLES.items():
        # Try uncompressed first
        db_path = backup_dir / db_name
        gz_path = backup_dir / f"{db_name}.gz"

        if db_path.exists():
            result = verify_database(db_path, expected_tables)
        elif gz_path.exists():
            # Decompress to temp file. A truncated or corrupt .db.gz will
            # raise gzip.BadGzipFile — we catch that here and record it as
            # a verification failure rather than crashing the whole run,
            # because backup.sh invokes this script automatically and a
            # crash would mask the very problem the verifier exists to
            # surface.
            try:
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                    with gzip.open(gz_path, 'rb') as f:
                        tmp.write(f.read())
                    tmp_path = Path(tmp.name)
                try:
                    result = verify_database(tmp_path, expected_tables)
                finally:
                    tmp_path.unlink()
                result["file"] = f"{db_name}.gz (decompressed)"
            except (gzip.BadGzipFile, OSError) as e:
                result = {
                    "file": f"{db_name}.gz",
                    "size_bytes": gz_path.stat().st_size if gz_path.exists() else 0,
                    "valid": False,
                    "errors": [f"Could not decompress .gz: {type(e).__name__}: {e}"],
                    "tables": {},
                }
        else:
            # Database not in backup — might be OK if it didn't exist at backup time
            result = {
                "file": db_name,
                "valid": True,
                "skipped": True,
                "errors": ["Not found in backup (may not have existed)"],
                "tables": {},
            }

        if not result.get("valid", False):
            results["all_valid"] = False
        results["databases"].append(result)

    return results

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_backup.py <backup_dir>")
        print("Example: python scripts/verify_backup.py backups/20250101_120000")
        sys.exit(1)

    backup_dir = Path(sys.argv[1])
    results = verify_backup_dir(backup_dir)

    # Print human-readable report
    print(f"\n{'='*60}")
    print(f"Backup Verification Report")
    print(f"{'='*60}")
    print(f"Directory: {results.get('backup_dir', 'N/A')}")
    print(f"Verified: {results.get('verified_at', 'N/A')}")
    print(f"Overall: {'\u2713 VALID' if results.get('all_valid') else '\u2717 INVALID'}")
    print(f"{'='*60}\n")

    for db in results.get("databases", []):
        status = "\u2713" if db.get("valid") else "\u2717"
        if db.get("skipped"):
            status = "\u2298"
        size_mb = db.get("size_bytes", 0) / 1024 / 1024
        print(f"  {status} {db['file']:<40} ({size_mb:.1f}MB)")

        if db.get("errors"):
            for err in db["errors"]:
                print(f"      Error: {err}")

        for table, count in db.get("tables", {}).items():
            print(f"      {table}: {count} rows")

    print(f"\n{'='*60}")

    # Also output JSON
    json_path = backup_dir / "verification_report.json"
    try:
        with open(json_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"JSON report: {json_path}")
    except OSError as e:
        # If backup_dir doesn't exist (early-return case above), we can't
        # write the JSON report there. Emit a warning instead of crashing.
        print(f"WARNING: could not write JSON report to {json_path}: {e}")

    sys.exit(0 if results.get("all_valid") else 1)

if __name__ == "__main__":
    main()
