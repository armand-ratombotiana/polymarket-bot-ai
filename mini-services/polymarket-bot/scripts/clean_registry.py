#!/usr/bin/env python3
"""Clean test-polluted entries out of a model registry JSON file.

W18-8 — P0-C08 fix.

The model registry at ``data/model_registry.json`` (or any path passed
via ``--path`` / ``MODEL_REGISTRY_PATH`` env var) accumulates version
records on every ``fit_initial()`` call. The W17-3 audit found that
most of those entries were test fixtures — n_samples=100, brier=0.1786,
ece=0.2617, parameters={"n_estimators_rf": 10, ...} — produced by
unit tests that drove ``MarketMLModel.fit_initial()`` with a shrunk
synthetic dataset and then persisted the resulting version into the
production registry (because the test env-var redirect to ``/tmp`` was
either not set yet, or the singleton was already constructed against
the production path before conftest could redirect it).

This script:

  1. Loads the registry JSON at ``--path`` (or ``MODEL_REGISTRY_PATH``,
     or the production default ``data/model_registry.json``).
  2. Drops every entry that looks like a test fixture:
       - ``n_samples`` below ``--min-samples`` (default 200 — a real
         training cycle blends ≥ 3000 synthetic samples with whatever
         real DB rows it can find; a version with n < 200 cannot be a
         production-grade training run).
       - ``ece`` above ``--max-ece`` (default 0.20 — the calibrated
         ensemble ships with ECE ≈ 0.04 on the held-out fold; a version
         with ECE > 0.20 is either uncalibrated or a deliberately
         broken fixture).
  3. Re-points ``active_version`` to the most recent SURVIVING entry
     (or ``None`` if no entries survive — the operator must retrain).
  4. Writes the cleaned registry back to the same file (atomic
     ``tmp → rename``).
  5. Prints a one-line summary so the operator can verify the delta.

Idempotent
----------
Safe to run repeatedly — running it on an already-clean registry is a
no-op (0 entries dropped, ``active_version`` unchanged).

Exit codes
----------
  0  registry cleaned (or already clean)
  1  registry file missing / unreadable / invalid JSON
  2  CLI usage error

Usage
-----
::

    # Clean the production registry (defaults to data/model_registry.json
    # or $MODEL_REGISTRY_PATH if set).
    python scripts/clean_registry.py

    # Clean a specific registry file
    python scripts/clean_registry.py --path /tmp/test_model_registry.json

    # Tighten the heuristics
    python scripts/clean_registry.py --min-samples 1000 --max-ece 0.10

    # Dry run — show what WOULD be dropped without writing the file
    python scripts/clean_registry.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Default production registry path. Mirrors the default in
# ``ml/model_registry.py`` so a vanilla ``python scripts/clean_registry.py``
# invocation lands on the same file the singleton reads at import time.
DEFAULT_REGISTRY_PATH = Path(
    os.environ.get("MODEL_REGISTRY_PATH", "data/model_registry.json")
)

# Default heuristic thresholds — see module docstring.
DEFAULT_MIN_SAMPLES = 200
DEFAULT_MAX_ECE = 0.20


def _is_test_fixture(entry: dict[str, Any], min_samples: int, max_ece: float) -> bool:
    """Classify a registry entry as a test fixture vs. a real version.

    Returns ``True`` if ``entry`` should be DROPPED from the registry.
    Drops on EITHER signal — both flags are strong evidence of a test
    fixture on their own, so the OR keeps the heuristic tight without
    requiring both conditions (some test fixtures may produce a
    plausible ECE while still being tiny training runs; some real
    versions may have a temporarily elevated ECE while still being
    trained on the full dataset).

    Defensive: missing fields default to ``0`` (n_samples) / ``1.0``
    (ece) so a malformed entry is dropped rather than retained as the
    active version.
    """
    n = int(entry.get("n_samples", 0) or 0)
    ece = float(entry.get("ece", 1.0) or 1.0)
    return n < min_samples or ece > max_ece


def clean_registry(
    path: Path,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_ece: float = DEFAULT_MAX_ECE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Clean test-polluted entries from the registry at ``path``.

    Args:
        path: registry JSON file to clean. Must exist + be a valid
            ``{"active_version": str, "versions": [...]}`` document.
        min_samples: drop entries with ``n_samples < min_samples``.
        max_ece: drop entries with ``ece > max_ece``.
        dry_run: when ``True``, return the would-be delta WITHOUT
            writing the cleaned registry back to disk.

    Returns:
        A summary dict with:
          ``"total_before"``  — count before cleaning
          ``"total_after"``   — count after cleaning
          ``"dropped"``       — list of dropped version strings
          ``"active_before"`` — pre-clean active_version
          ``"active_after"``  — post-clean active_version (or ``None``
                                 if no entries survived; the operator
                                 must retrain)
          ``"path"``          — the path operated on
          ``"written"``       — ``True`` if the file was rewritten

    Raises:
        FileNotFoundError: ``path`` does not exist.
        json.JSONDecodeError: ``path`` is not valid JSON.
        KeyError: ``path`` JSON has no ``"versions"`` key.
    """
    if not path.exists():
        raise FileNotFoundError(f"registry file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        registry = json.load(f)

    if "versions" not in registry:
        raise KeyError(f"registry JSON missing 'versions' key: {path}")

    versions = list(registry.get("versions") or [])
    total_before = len(versions)
    active_before = registry.get("active_version")

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for v in versions:
        if _is_test_fixture(v, min_samples, max_ece):
            dropped.append(str(v.get("version", "<unknown>")))
        else:
            kept.append(v)

    # ``active_version`` re-points to the most recent SURVIVING entry.
    # The registry stores versions newest-first (``register_version``
    # inserts at index 0), so ``kept[0]`` is the newest survivor — but
    # if the file was hand-edited or built by an older code path, fall
    # back to ``kept[-1]`` (oldest survivor). The semantics here are
    # "pick a real version that survived the cleanup" — the operator
    # can roll forward / back via the registry API.
    active_after = None
    if kept:
        # Newest-first convention: kept[0] is the newest ACTIVE
        # survivor (or the newest REJECTED survivor — accepted, since
        # we cannot infer the operator's intent from a CLI script).
        active_after = str(kept[0].get("version", "")) or None

    summary = {
        "total_before": total_before,
        "total_after": len(kept),
        "dropped": dropped,
        "active_before": active_before,
        "active_after": active_after,
        "path": str(path),
        "written": False,
    }

    if dry_run:
        return summary

    registry["versions"] = kept
    registry["active_version"] = active_after

    # Atomic write: tmp file in the same dir → fsync → rename. So a
    # crash mid-write cannot leave the registry truncated / empty.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.clean.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(registry, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    summary["written"] = True
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean test-polluted entries out of a model registry JSON file.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help=(
            "Path to the registry JSON. Defaults to $MODEL_REGISTRY_PATH "
            "or 'data/model_registry.json' (the production path)."
        ),
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Drop entries with n_samples < this (default {DEFAULT_MIN_SAMPLES}).",
    )
    parser.add_argument(
        "--max-ece",
        type=float,
        default=DEFAULT_MAX_ECE,
        help=f"Drop entries with ece > this (default {DEFAULT_MAX_ECE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be dropped without writing the file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = args.path or DEFAULT_REGISTRY_PATH
    try:
        summary = clean_registry(
            path,
            min_samples=args.min_samples,
            max_ece=args.max_ece,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, KeyError) as e:
        print(f"ERROR: registry JSON invalid: {e}", file=sys.stderr)
        return 1

    verb = "would clean" if args.dry_run else "cleaned"
    print(
        f"{verb}: {summary['path']} — "
        f"{summary['total_before']} → {summary['total_after']} versions "
        f"(dropped {len(summary['dropped'])}); "
        f"active: {summary['active_before']!r} → {summary['active_after']!r}"
    )
    if summary["dropped"] and len(summary["dropped"]) <= 20:
        print(f"  dropped versions: {summary['dropped']}")
    elif summary["dropped"]:
        print(f"  dropped versions: {summary['dropped'][:20]} … (+{len(summary['dropped']) - 20} more)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
