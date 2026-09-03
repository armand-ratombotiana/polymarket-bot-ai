#!/bin/bash
# backup.sh — Backup all SQLite databases for the Polymarket bot.
#
# Uses the SQLite Online Backup API (`sqlite3 .backup` or the Python
# `sqlite3.Connection.backup()` equivalent) so it is SAFE to run while
# the bot is live — no read locks held on the source DB, no torn writes.
#
# Backups land in timestamped subdirs of $BACKUP_DIR, gzipped, with a
# MANIFEST.txt listing contents. Backups older than $RETENTION_DAYS
# are pruned at the end of every run.
#
# Env vars (all optional, sensible defaults):
#   BOT_DATA_DIR     Source data dir (default: polymarket-bot/data)
#   BACKUP_DIR       Destination root (default: /home/z/my-project/backups)
#   RETENTION_DAYS   Days to keep (default: 14)
#   LOG_FILE         Optional log file path (default: stderr only)
#
# Cron usage (every 6 hours at minute 15):
#   15 */6 * * * /home/z/my-project/scripts/backup.sh >> /home/z/my-project/logs/backup.cron.log 2>&1
set -euo pipefail

DATA_DIR="${BOT_DATA_DIR:-/home/z/my-project/mini-services/polymarket-bot/data}"
BACKUP_DIR="${BACKUP_DIR:-/home/z/my-project/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
LOG_FILE="${LOG_FILE:-}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_PATH="$BACKUP_DIR/$TIMESTAMP"

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg"
  # NB: a plain `[ -n "$LOG_FILE" ] && { ... }` would short-circuit to false
  # when LOG_FILE is empty, and under `set -e` that kills the script.
  # Use an explicit `if` so the function always returns 0.
  if [ -n "$LOG_FILE" ]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$msg" >> "$LOG_FILE"
  fi
}

# --- sqlite3 backend selection ----------------------------------------------
# The Polymarket bot runs on hosts that may not have the `sqlite3` CLI
# installed (e.g. slim Docker images). Both the CLI `.backup` command and
# the Python `sqlite3.Connection.backup()` method call the SAME underlying
# SQLite Online Backup API (https://sqlite.org/backup.html), so we fall
# back to the Python module when the CLI is missing. Output is byte-identical.
sqlite_backup() {
  local src="$1" dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'"
  else
    python3 - "$src" "$dst" <<'PY'
import sqlite3, sys, os
src, dst = sys.argv[1], sys.argv[2]
# Python's Connection.backup() requires a Connection (not a filename).
# Open a fresh DB at `dst` (creating it), then copy `src` into it via the
# Online Backup API. This is byte-equivalent to `sqlite3 src .backup 'dst'`.
if os.path.exists(dst):
    os.remove(dst)         # `.backup` overwrites, so mimic that.
src_conn = sqlite3.connect(src)
dst_conn = sqlite3.connect(dst)
try:
    src_conn.backup(dst_conn)   # default name="main" copies the main schema
finally:
    dst_conn.close()
    src_conn.close()
PY
  fi
}

mkdir -p "$BACKUP_PATH"
log "Starting backup to $BACKUP_PATH"
log "  data_dir=$DATA_DIR"
log "  backup_dir=$BACKUP_DIR"
log "  retention_days=$RETENTION_DAYS"

# --- Backup each SQLite database --------------------------------------------
shopt -s nullglob
backup_count=0
for db_file in "$DATA_DIR"/*.db; do
  [ -f "$db_file" ] || continue
  db_name=$(basename "$db_file")
  log "  Backing up $db_name ..."
  # The .backup command writes a fresh file; if it fails we abort the whole
  # script (set -e) so partial backups are visible rather than silently
  # shipped as "complete".
  sqlite_backup "$db_file" "$BACKUP_PATH/$db_name"
  gzip -f "$BACKUP_PATH/$db_name"
  backup_count=$((backup_count + 1))
done

# --- Backup config files ----------------------------------------------------
config_count=0
for config_file in "$DATA_DIR"/*.json; do
  [ -f "$config_file" ] || continue
  cp "$config_file" "$BACKUP_PATH/"
  config_count=$((config_count + 1))
done

# npz (vector store) is large and rarely changes; copy if present.
for npz_file in "$DATA_DIR"/*.npz; do
  [ -f "$npz_file" ] || continue
  cp "$npz_file" "$BACKUP_PATH/"
  config_count=$((config_count + 1))
done

# --- Manifest ---------------------------------------------------------------
{
  echo "Backup created at $(date)"
  echo "Host: $(hostname)"
  echo "Data dir: $DATA_DIR"
  echo "Backup dir: $BACKUP_PATH"
  echo "Databases: $backup_count"
  echo "Config files: $config_count"
  echo
  echo "Contents:"
  ls -lh "$BACKUP_PATH"
  echo
  echo "SHA256 of each .db.gz:"
  ( cd "$BACKUP_PATH" && sha256sum *.db.gz 2>/dev/null || true )
} > "$BACKUP_PATH/MANIFEST.txt"

log "  Backed up $backup_count DB(s), $config_count config file(s)"

# --- Prune old backups ------------------------------------------------------
log "Pruning backups older than $RETENTION_DAYS days..."
# -mindepth 1 so we never delete the BACKUP_DIR root itself.
find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +$RETENTION_DAYS \
  -exec rm -rf {} \; 2>/dev/null || true

remaining=$(find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
log "Backup complete: $BACKUP_PATH ($remaining backup(s) retained)"

# --- Verify the backup just created -----------------------------------------
# We auto-verify every fresh backup so a corrupt backup is caught at write
# time, not at restore time when it's too late. Verification failure is
# logged as a WARNING (not fatal) so a single bad DB doesn't block the rest
# of the cron run — the operator will see the warning in the cron log and
# can investigate. The verifier writes a verification_report.json inside
# the backup dir for offline inspection.
VERIFY_SCRIPT="$(dirname "$0")/verify_backup.py"
if [ -f "$VERIFY_SCRIPT" ]; then
  log "Verifying backup integrity..."
  # The verifier prints a human-readable report to stdout and writes a
  # verification_report.json inside the backup dir. We let stdout/stderr
  # flow naturally so the cron wrapper (2>&1) captures them into the cron
  # log alongside the rest of this script's output.
  if python3 "$VERIFY_SCRIPT" "$BACKUP_PATH"; then
    log "  Verification: PASS"
  else
    log "  WARNING: Backup verification failed — see verification_report.json"
    log "           in $BACKUP_PATH for details."
  fi
else
  log "  (verify_backup.py not found at $VERIFY_SCRIPT — skipping auto-verify)"
fi
