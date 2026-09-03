#!/bin/bash
# setup-cron.sh — Install cron entries for backup / maintenance / health check.
#
# Installs three cron jobs (idempotent — re-running replaces the existing
# managed block rather than duplicating it):
#
#   Schedule         Job                             Log
#   ---------------  ------------------------------  ------------------------------
#   15 */6 * * *     backup.sh                       logs/backup.cron.log
#   0 3 * * *        db-maintenance.sh               logs/db-maintenance.cron.log
#   */5 * * * *      health-check.sh                 logs/health-check.cron.log
#
# The managed block is wrapped with marker lines so re-running this script
# surgically replaces ONLY the polymarket-bot entries — your other crontab
# entries (editor temp files, other apps) are preserved.
#
# Env vars:
#   PROJECT_ROOT  Project root (default: /home/z/my-project)
#   LOG_DIR       Cron log dir (default: $PROJECT_ROOT/logs)
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/z/my-project}"
SCRIPTS_DIR="$PROJECT_ROOT/scripts"
LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/logs}"
MARKER_BEGIN="# >>> polymarket-bot maintenance (W12-2) >>>"
MARKER_END="# <<< polymarket-bot maintenance (W12-2) <<<"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }

mkdir -p "$LOG_DIR"

# --- preflight checks -------------------------------------------------------
if ! command -v crontab >/dev/null 2>&1; then
  err "crontab command not found. Install a cron daemon first:"
  err "  Debian/Ubuntu: sudo apt-get install -y cron && sudo systemctl enable --now cron"
  err "  RHEL/Fedora:   sudo dnf install -y cronie && sudo systemctl enable --now crond"
  exit 1
fi

for script in backup.sh db-maintenance.sh health-check.sh; do
  if [ ! -f "$SCRIPTS_DIR/$script" ]; then
    err "Missing script: $SCRIPTS_DIR/$script"
    exit 1
  fi
done
chmod +x "$SCRIPTS_DIR"/backup.sh "$SCRIPTS_DIR"/db-maintenance.sh "$SCRIPTS_DIR"/health-check.sh

# --- build new crontab ------------------------------------------------------
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Preserve existing crontab minus any prior managed block.
if crontab -l >/dev/null 2>&1; then
  crontab -l | awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
    $0==b {inblock=1; next}
    $0==e {inblock=0; next}
    !inblock {print}
  ' > "$TMP"
fi

# Append fresh managed block.
{
  echo "$MARKER_BEGIN"
  echo "# Backup every 6 hours at minute 15 (00:15, 06:15, 12:15, 18:15)"
  echo "15 */6 * * * $SCRIPTS_DIR/backup.sh >> $LOG_DIR/backup.cron.log 2>&1"
  echo "# DB maintenance daily at 3:00 AM (lowest-traffic hour)"
  echo "0 3 * * * $SCRIPTS_DIR/db-maintenance.sh >> $LOG_DIR/db-maintenance.cron.log 2>&1"
  echo "# Health check every 5 minutes"
  echo "*/5 * * * * $SCRIPTS_DIR/health-check.sh >> $LOG_DIR/health-check.cron.log 2>&1"
  echo "$MARKER_END"
} >> "$TMP"

# --- install ----------------------------------------------------------------
crontab "$TMP"
log "Cron entries installed successfully."
log "Scripts dir: $SCRIPTS_DIR"
log "Log dir:      $LOG_DIR"
echo
echo "Current crontab:"
echo "----------------------------------------"
crontab -l
echo "----------------------------------------"
echo
echo "Verify the cron daemon is running:"
echo "  systemctl status cron 2>/dev/null || systemctl status crond 2>/dev/null"
echo
echo "To remove all polymarket-bot cron entries later:"
echo "  crontab -l | sed '/$MARKER_BEGIN/,/$MARKER_END/d' | crontab -"
