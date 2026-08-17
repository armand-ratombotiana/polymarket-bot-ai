"""
core/safety.py — Durable Kill Switch.

The kill switch is file-backed so it survives process restarts and container
recycles. Activation writes a marker file, deactivation removes it. Every order
path consults it; the watchdog can also activate it on critical tripwires.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

KILL_SWITCH_PATH = Path(os.environ.get("KILL_SWITCH_PATH", "/app/data/kill_switch"))
ACTIVATION_REASON_FILE = Path(os.environ.get("KILL_SWITCH_REASON_PATH", "/app/data/kill_switch.reason"))


def kill_switch_file_exists() -> bool:
    """Durable marker: a kill switch that survives a restart."""
    try:
        return KILL_SWITCH_PATH.exists()
    except OSError:
        log.error("[safety] Cannot stat kill switch path %s", KILL_SWITCH_PATH)
        return False


def write_kill_switch(reason: str) -> None:
    """Write the durable marker file (fail-loud on error)."""
    try:
        KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH_PATH.write_text(reason or "manual", encoding="utf-8")
        ACTIVATION_REASON_FILE.write_text(reason or "manual", encoding="utf-8")
    except OSError as e:
        log.critical("[safety] FAILED to write durable kill switch (%s): %s", KILL_SWITCH_PATH, e)
        raise


def clear_kill_switch() -> None:
    """Remove the durable marker (explicit re-arm, also clears activation reason)."""
    try:
        KILL_SWITCH_PATH.unlink(missing_ok=True)
        ACTIVATION_REASON_FILE.unlink(missing_ok=True)
    except OSError as e:
        log.error("[safety] Failed to clear kill switch (%s): %s", KILL_SWITCH_PATH, e)
        raise


def read_kill_switch_reason() -> str:
    try:
        if ACTIVATION_REASON_FILE.exists():
            return ACTIVATION_REASON_FILE.read_text(encoding="utf-8").strip() or "unspecified"
    except OSError:
        pass
    if settings is not None:
        return getattr(settings, "_kill_reason", "unspecified")
    return "unspecified"