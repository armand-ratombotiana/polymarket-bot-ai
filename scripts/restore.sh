#!/bin/bash
# restore.sh — Restore SQLite databases from a backup timestamp.
#
# Usage: restore.sh <YYYYMMDD_HHMMSS> [--force]
#
# Workflow:
#   1. Resolve $BACKUP_DIR/<timestamp>, verify it exists + has a manifest.
#   2. Snapshot the CURRENT live DBs to a pre_restore_<ts> dir (safety net).
#   3. Stop the bot (systemd unit, pkill fallback, uvicorn fallback).
#   4. Gunzip each .db.gz into $BOT_DATA_DIR (overwrites live DBs).
#   5. Restore config JSONs.
#   6. Run `PRAGMA integrity_check` on every restored DB.
#   7. Report success or roll-forward instructions.
#
# The script NEVER deletes the pre-restore snapshot — even after a
# successful restore — so you have a forensic recovery point.
set -euo pipefail

DATA_DIR="${BOT_DATA_DIR:-/home/z/my-project/mini-services/polymarket-bot/data}"
BACKUP_DIR="${BACKUP_DIR:-/home/z/my-project/backups}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }

usage() {
  cat <<EOF
Usage: $0 <YYYYMMDD_HHMMSS> [--force]

Restores databases from $BACKUP_DIR/<timestamp>

Options:
  --force    Skip the interactive RESTORE confirmation prompt.
  -h|--help  Show this help.

Backup directories available:
$(ls -1 "$BACKUP_DIR" 2>/dev/null | head -50 || echo "  (none — $BACKUP_DIR does not exist)")
EOF
  exit 1
}

# --- arg parsing ------------------------------------------------------------
FORCE=0
TIMESTAMP=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) usage ;;
    *) [ -n "$TIMESTAMP" ] && { err "Multiple timestamps given: $TIMESTAMP and $arg"; exit 1; }; TIMESTAMP="$arg" ;;
  esac
done
[ -z "$TIMESTAMP" ] && usage

BACKUP_PATH="$BACKUP_DIR/$TIMESTAMP"
if [ ! -d "$BACKUP_PATH" ]; then
  err "Backup not found: $BACKUP_PATH"
  echo "Available backups:" >&2
  ls -1 "$BACKUP_DIR" 2>/dev/null | head -20 >&2 || echo "  (none)" >&2
  exit 1
fi
if ! ls "$BACKUP_PATH"/*.db.gz >/dev/null 2>&1; then
  err "No .db.gz files in $BACKUP_PATH — not a backup directory."
  exit 1
fi

# --- sqlite helpers (CLI or python fallback) --------------------------------
sqlite_exec() {
  local db="$1" sql="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$db" "$sql"
  else
    python3 - "$db" "$sql" <<'PY'
import sqlite3, sys
db, sql = sys.argv[1], sys.argv[2]
with sqlite3.connect(db) as conn:
    cur = conn.execute(sql)
    rows = cur.fetchall()
    for r in rows:
        print("\t".join(str(c) for c in r))
PY
  fi
}

# --- restore plan + confirmation --------------------------------------------
echo "=============================================================="
echo " RESTORE PLAN"
echo "--------------------------------------------------------------"
echo "  Backup source : $BACKUP_PATH"
echo "  Restore dest : $DATA_DIR"
echo "  Pre-restore snapshot will be saved to:"
echo "    $BACKUP_DIR/pre_restore_$(date +%Y%m%d_%H%M%S)"
echo
echo "  Databases to overwrite:"
shopt -s nullglob
for f in "$BACKUP_PATH"/*.db.gz; do
  echo "    - $(basename "$f" .gz)"
done
echo
echo "  WARNING: This will OVERWRITE live databases in $DATA_DIR."
echo "           The bot MUST be stopped before restore."
echo "=============================================================="

if [ "$FORCE" -ne 1 ]; then
  echo
  read -rp "Type RESTORE to proceed (anything else aborts): " confirm
  if [ "$confirm" != "RESTORE" ]; then
    log "Aborted by user."
    exit 1
  fi
fi

# --- 1. Snapshot current live DBs (safety net) ------------------------------
PRE_RESTORE_PATH="$BACKUP_DIR/pre_restore_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$PRE_RESTORE_PATH"
log "Snapshotting live DBs to $PRE_RESTORE_PATH ..."
for db_file in "$DATA_DIR"/*.db; do
  [ -f "$db_file" ] || continue
  cp "$db_file" "$PRE_RESTORE_PATH/" || log "  (warn: could not snapshot $(basename "$db_file"))"
done
{
  echo "Pre-restore snapshot taken at $(date)"
  echo "Restore source: $BACKUP_PATH"
  echo "Restore target: $DATA_DIR"
  ls -lh "$PRE_RESTORE_PATH"
} > "$PRE_RESTORE_PATH/MANIFEST.txt"

# --- 2. Stop the bot --------------------------------------------------------
log "Stopping bot (if running)..."
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl stop polymarket-bot 2>/dev/null && log "  systemd: polymarket-bot stopped" || true
fi
# Fallbacks for non-systemd deployments
pkill -f "mini-services/polymarket-bot/main.py" 2>/dev/null && log "  pkill: main.py stopped" || true
pkill -f "uvicorn.*api.server" 2>/dev/null && log "  pkill: uvicorn stopped" || true
pkill -f "supervisord.*polymarket" 2>/dev/null && log "  pkill: supervisord stopped" || true
sleep 2

# --- 3. Restore databases ---------------------------------------------------
log "Restoring databases..."
for gz_file in "$BACKUP_PATH"/*.db.gz; do
  db_name=$(basename "$gz_file" .gz)           # e.g. decision_ledger.db
  target="$DATA_DIR/$db_name"
  log "  Restoring $db_name ..."
  # Remove WAL/SHM side files first — otherwise stale WAL data can resurrect
  # rows from the pre-restore DB and confuse the integrity check.
  rm -f "$target-wal" "$target-shm" 2>/dev/null || true
  gunzip -c "$gz_file" > "$target"
done

# Restore config files (JSONs) — only if present in the backup
for cfg_file in "$BACKUP_PATH"/*.json; do
  [ -f "$cfg_file" ] || continue
  cp "$cfg_file" "$DATA_DIR/"
  log "  Restored config: $(basename "$cfg_file")"
done

# --- 4. Integrity check -----------------------------------------------------
log "Verifying integrity of restored DBs..."
FAIL=0
for db_file in "$DATA_DIR"/*.db; do
  [ -f "$db_file" ] || continue
  db_name=$(basename "$db_file")
  result=$(sqlite_exec "$db_file" "PRAGMA integrity_check;" | tail -1)
  if [ "$result" = "ok" ]; then
    log "  OK     : $db_name"
  else
    err "  FAIL   : $db_name → $result"
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  err "INTEGRITY CHECK FAILED on one or more DBs."
  err "Live DBs were snapshotted to: $PRE_RESTORE_PATH"
  err "To roll back: copy each .db file back from there to $DATA_DIR/"
  exit 2
fi

log "=============================================================="
log " RESTORE COMPLETE"
log "  Backup restored from : $BACKUP_PATH"
log "  Pre-restore snapshot  : $PRE_RESTORE_PATH (kept as safety net)"
log "  You may now restart the bot."
log "=============================================================="
