#!/bin/bash
# health-check.sh — System health check for the Polymarket bot stack.
#
# Checks:
#   1. Frontend (Next.js) on :3000 responds 200
#   2. Backend (FastAPI) on :8080/api/health responds 200
#   3. Disk usage on the project volume (< 85% ok, < 95% degraded, >= 95% fail)
#   4. Memory usage (< 85% ok, < 95% degraded, >= 95% fail)
#   5. Each SQLite DB file is non-zero and reports `PRAGMA integrity_check=ok`
#
# Output: a single JSON object on stdout, e.g.
#   {
#     "timestamp": "2026-09-03T14:20:00Z",
#     "status": "healthy|degraded|unhealthy",
#     "checks": [ {name, status, detail}, ... ],
#     "databases": [ {name, bytes, mb, integrity}, ... ]
#   }
#
# Exit code:
#   0 — healthy
#   1 — degraded (warnings) OR unhealthy (any check failed)
#
# Cron usage (every 5 minutes):
#   */5 * * * * /home/z/my-project/scripts/health-check.sh >> /home/z/my-project/logs/health-check.cron.log 2>&1
#
# Set BOT_DATA_DIR / FRONTEND_URL / BACKEND_URL env vars to override defaults.

# NOTE: NO `set -e` — a health check must NEVER crash; every check is wrapped
# in defensive conditionals so a missing tool (free/df) becomes a "warn"
# status rather than a script crash.
set -uo pipefail

FRONTEND_URL="${FRONTEND_URL:-http://localhost:3000}"
BACKEND_URL="${BACKEND_URL:-http://localhost:8080}"
DATA_DIR="${BOT_DATA_DIR:-/home/z/my-project/mini-services/polymarket-bot/data}"
DISK_PATH="${DISK_PATH:-/home/z/my-project}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
DISK_CRIT_PCT="${DISK_CRIT_PCT:-95}"
MEM_WARN_PCT="${MEM_WARN_PCT:-85}"
MEM_CRIT_PCT="${MEM_CRIT_PCT:-95}"

overall="healthy"
checks_json="[]"
databases_json="[]"

# Add a check record (status ∈ ok|warn|fail; warn → degraded, fail → unhealthy)
add_check() {
  local name="$1" status="$2" detail="$3"
  # NOTE: -n (null input) is critical — without it, jq reads from stdin and
  # blocks the script when run from cron / a non-terminal context.
  checks_json=$(jq -cn --argjson arr "$checks_json" \
                     --arg n "$name" --arg s "$status" --arg d "$detail" \
                     '$arr + [{name:$n, status:$s, detail:$d}]')
  case "$status" in
    warn) [ "$overall" = "healthy" ] && overall="degraded" ;;
    fail) overall="unhealthy" ;;
  esac
}

# Run a SQLite SQL statement via CLI or python fallback.
sqlite_exec() {
  local db="$1" sql="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$db" "$sql" 2>/dev/null
  else
    python3 - "$db" "$sql" 2>/dev/null <<'PY'
import sqlite3, sys
db, sql = sys.argv[1], sys.argv[2]
with sqlite3.connect(db, timeout=10) as conn:
    cur = conn.execute(sql)
    rows = cur.fetchall()
    for r in rows:
        print("\t".join(str(c) for c in r))
PY
  fi
}

# --- 1. Frontend -------------------------------------------------------------
# curl always writes %{http_code} (000 on failure), so the `|| true` is just to
# satisfy set -uo pipefail — we never want a curl hiccup to crash the script.
fe_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$FRONTEND_URL" 2>/dev/null || true)
[ -z "$fe_code" ] && fe_code="000"
case "$fe_code" in
  200) add_check "frontend" "ok"   "HTTP 200 from $FRONTEND_URL" ;;
  000) add_check "frontend" "fail" "no response from $FRONTEND_URL (connection refused / timeout)" ;;
  *)   add_check "frontend" "fail" "HTTP $fe_code from $FRONTEND_URL" ;;
esac

# --- 2. Backend --------------------------------------------------------------
be_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$BACKEND_URL/api/health" 2>/dev/null || true)
[ -z "$be_code" ] && be_code="000"
case "$be_code" in
  200) add_check "backend" "ok"   "HTTP 200 from $BACKEND_URL/api/health" ;;
  000) add_check "backend" "fail" "no response from $BACKEND_URL/api/health (connection refused / timeout)" ;;
  *)   add_check "backend" "fail" "HTTP $be_code from $BACKEND_URL/api/health" ;;
esac

# --- 3. Disk space -----------------------------------------------------------
disk_line=$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $5}')
disk_use_pct="${disk_line%\%}"   # strip trailing % if present
if [ -z "$disk_use_pct" ]; then
  add_check "disk" "warn" "could not determine disk usage (df unavailable)"
elif [ "$disk_use_pct" -ge "$DISK_CRIT_PCT" ]; then
  add_check "disk" "fail" "${disk_use_pct}% used (>= ${DISK_CRIT_PCT}%)"
elif [ "$disk_use_pct" -ge "$DISK_WARN_PCT" ]; then
  add_check "disk" "warn" "${disk_use_pct}% used (>= ${DISK_WARN_PCT}%)"
else
  add_check "disk" "ok" "${disk_use_pct}% used"
fi

# --- 4. Memory ---------------------------------------------------------------
if command -v free >/dev/null 2>&1; then
  # Linux: free -m, columns are total used free shared buff/cache available
  mem_total=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}')
  mem_avail=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}')
  if [ -n "$mem_total" ] && [ -n "$mem_avail" ] && [ "$mem_total" -gt 0 ] 2>/dev/null; then
    mem_used=$((mem_total - mem_avail))
    mem_use_pct=$(( mem_used * 100 / mem_total ))
    if [ "$mem_use_pct" -ge "$MEM_CRIT_PCT" ]; then
      add_check "memory" "fail" "${mem_use_pct}% used (${mem_used}MB / ${mem_total}MB available ${mem_avail}MB)"
    elif [ "$mem_use_pct" -ge "$MEM_WARN_PCT" ]; then
      add_check "memory" "warn" "${mem_use_pct}% used (${mem_used}MB / ${mem_total}MB available ${mem_avail}MB)"
    else
      add_check "memory" "ok" "${mem_use_pct}% used (${mem_used}MB / ${mem_total}MB available ${mem_avail}MB)"
    fi
  else
    add_check "memory" "warn" "could not parse free output"
  fi
else
  add_check "memory" "warn" "free command unavailable (non-Linux host?)"
fi

# --- 5. Database file sizes + integrity -------------------------------------
if [ ! -d "$DATA_DIR" ]; then
  add_check "databases" "fail" "data dir $DATA_DIR missing"
else
  shopt -s nullglob
  for db_file in "$DATA_DIR"/*.db; do
    [ -f "$db_file" ] || continue
    db_name=$(basename "$db_file")
    size_bytes=$(stat -c%s "$db_file" 2>/dev/null || stat -f%z "$db_file" 2>/dev/null || echo 0)
    size_mb=$(awk -v b="$size_bytes" 'BEGIN{printf "%.2f", b/1024/1024}')

    # integrity_check (skip if 0-byte — that's a "fail" on its own)
    if [ "$size_bytes" -eq 0 ]; then
      integrity="empty"
      add_check "db:$db_name" "fail" "0 bytes — empty/corrupt"
    else
      integrity=$(sqlite_exec "$db_file" "PRAGMA integrity_check;" | tail -1)
      [ -z "$integrity" ] && integrity="unknown"
      if [ "$integrity" = "ok" ]; then
        add_check "db:$db_name" "ok" "${size_mb}MB, integrity OK"
      else
        add_check "db:$db_name" "fail" "${size_mb}MB, integrity: $integrity"
      fi
    fi

    databases_json=$(jq -cn --argjson arr "$databases_json" \
                          --arg n "$db_name" --argjson b "$size_bytes" --arg m "$size_mb" --arg i "$integrity" \
                          '$arr + [{name:$n, bytes:$b, mb:$m, integrity:$i}]')
  done
  shopt -u nullglob
fi

# --- Output JSON -------------------------------------------------------------
ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
jq -cn \
  --arg ts "$ts" \
  --arg status "$overall" \
  --argjson checks "$checks_json" \
  --argjson databases "$databases_json" \
  '{timestamp:$ts, status:$status, checks:$checks, databases:$databases}'

case "$overall" in
  healthy) exit 0 ;;
  *)       exit 1 ;;
esac
