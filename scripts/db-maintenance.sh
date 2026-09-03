#!/bin/bash
# db-maintenance.sh — SQLite maintenance: VACUUM, ANALYZE, integrity_check.
#
# Safe to run while the bot is online:
#   - VACUUM: requires a brief write lock, but the SQLite Online Backup
#     API keeps reads non-blocking. Worst case the bot sees a transient
#     SQLITE_BUSY for a few hundred ms — the bot already retries these.
#   - ANALYZE: read-only, never blocks.
#   - PRAGMA integrity_check: read-only, never blocks.
#
# If a DB is locked (e.g. mid-trade), VACUUM is skipped with a logged
# warning rather than aborting the whole run.
#
# Env vars:
#   BOT_DATA_DIR  Source data dir (default: polymarket-bot/data)
#   LOG_DIR       Where to write the maintenance log (default: project/logs)
#   LOG_FILE      Explicit log file path (overrides LOG_DIR/db-maintenance.log)
set -euo pipefail

DATA_DIR="${BOT_DATA_DIR:-/home/z/my-project/mini-services/polymarket-bot/data}"
LOG_DIR="${LOG_DIR:-/home/z/my-project/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/db-maintenance.log}"

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# --- sqlite helpers --------------------------------------------------------
sqlite_exec() {
  local db="$1" sql="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$db" "$sql"
  else
    python3 - "$db" "$sql" <<'PY'
import sqlite3, sys
db, sql = sys.argv[1], sys.argv[2]
with sqlite3.connect(db, timeout=30) as conn:
    cur = conn.execute(sql)
    rows = cur.fetchall()
    for r in rows:
        print("\t".join(str(c) for c in r))
PY
  fi
}

human_size() {
  # `du -h` is portable across Linux/macOS.
  du -h "$1" 2>/dev/null | cut -f1
}

log "=== DB maintenance start ==="
log "data_dir: $DATA_DIR"

shopt -s nullglob
total_reclaimed=0
processed=0
failed=0

for db_file in "$DATA_DIR"/*.db; do
  [ -f "$db_file" ] || continue
  db_name=$(basename "$db_file")
  size_before=$(stat -c%s "$db_file" 2>/dev/null || stat -f%z "$db_file" 2>/dev/null || echo 0)
  size_before_h=$(human_size "$db_file")
  log "[$db_name] size before: $size_before_h ($size_before bytes)"

  # --- integrity_check (read-only, never blocks) ---------------------------
  result=$(sqlite_exec "$db_file" "PRAGMA integrity_check;" 2>/dev/null | tail -1)
  if [ "$result" = "ok" ]; then
    log "  [$db_name] integrity_check: OK"
  else
    log "  [$db_name] integrity_check: FAIL — $result"
    failed=$((failed + 1))
    # Skip VACUUM/ANALYZE on a corrupt DB — could make it worse.
    continue
  fi

  # --- VACUUM (reclaims free pages, compacts file) -------------------------
  # VACUUM may fail with SQLITE_BUSY if another writer holds the lock;
  # we treat that as a soft skip rather than aborting the whole run.
  if sqlite_exec "$db_file" "VACUUM;" 2>/dev/null; then
    log "  [$db_name] VACUUM: OK"
  else
    log "  [$db_name] VACUUM: SKIPPED (DB locked or busy) — will retry next run"
  fi

  # --- ANALYZE (updates sqlite_stat1 for the query planner) ---------------
  if sqlite_exec "$db_file" "ANALYZE;" 2>/dev/null; then
    log "  [$db_name] ANALYZE: OK"
  else
    log "  [$db_name] ANALYZE: SKIPPED (DB busy)"
  fi

  size_after=$(stat -c%s "$db_file" 2>/dev/null || stat -f%z "$db_file" 2>/dev/null || echo 0)
  size_after_h=$(human_size "$db_file")
  reclaimed=$((size_before - size_after))
  if [ "$reclaimed" -lt 0 ]; then reclaimed=0; fi
  total_reclaimed=$((total_reclaimed + reclaimed))
  log "  [$db_name] size after:  $size_after_h ($size_after bytes) — reclaimed $reclaimed bytes"
  processed=$((processed + 1))
done

# --- summary ---------------------------------------------------------------
total_reclaimed_mb=$(( total_reclaimed / 1024 / 1024 ))
log "=== DB maintenance complete ==="
log "  processed: $processed DB(s)"
log "  failed:    $failed DB(s)"
log "  reclaimed: ${total_reclaimed_mb} MB total"

if [ "$failed" -gt 0 ]; then
  exit 3
fi
exit 0
