#!/bin/bash
# Verify all sidebar panels using agent-browser.
# Restarts the production server as needed since the dev/prod server
# gets killed by OOM after a few page requests.

set -u

LOG=/home/z/my-project/verify-panels.log
> "$LOG"

PANELS=(
  "Command Center"
  "Live Books"
  "Screener"
  "Order Flow"
  "Positions"
  "Orders"
  "Trades & Fills"
  "Capital Allocator"
  "Strategy Registry"
  "Arbitrage"
  "Performance"
  "Deep Analysis"
  "AI / ML Engine"
  "Copilot"
  "Shadow Inference"
  "ML Validation"
  "Performance Report"
  "Backtest Lab"
  "Attribution"
  "Execution Quality"
  "Closed Positions"
  "System Health"
  "Data Explorer"
  "Database"
  "Observability"
  "Retention"
  "Decision Ledger"
  "Safety Gate"
  "Rate Limits"
  "Audit Log"
)

start_server() {
  cd /home/z/my-project/.next/standalone
  pkill -9 -f "node.*server.js" 2>/dev/null
  sleep 1
  NODE_OPTIONS="--max-old-space-size=256" nohup setsid node server.js > /home/z/my-project/prod.log 2>&1 &
  disown
  sleep 3
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/)
  echo "$code"
}

server_alive() {
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:3000/)
  [ "$code" = "200" ]
}

verify_panel() {
  local panel_name="$1"
  local result=""
  local content=""
  local error_state=""
  local retries=0
  while [ $retries -lt 4 ]; do
    if ! server_alive; then
      echo "  Server down, restarting..." >> "$LOG"
      local start_code
      start_code=$(start_server)
      if [ "$start_code" != "200" ]; then
        echo "  Failed to restart (code=$start_code)" >> "$LOG"
        sleep 2
        retries=$((retries+1))
        continue
      fi
      # Re-open browser since server was down
      agent-browser open "http://localhost:3000/" 2>>"$LOG" >>"$LOG"
      sleep 3
    fi
    # Click panel by text
    agent-browser find text "$panel_name" click 2>>"$LOG" >>"$LOG"
    sleep 1
    # Check error boundary
    error_state=$(agent-browser eval "document.querySelector('.panel-error-boundary') ? 'ERROR' : 'OK'" 2>/dev/null | tr -d '"' | head -1)
    # Check content
    content=$(agent-browser eval "document.querySelector('.page-area') ? document.querySelector('.page-area').innerText.substring(0, 200) : 'NO CONTENT'" 2>/dev/null | tr -d '"' | head -1)
    if [ -n "$error_state" ] && [ "$error_state" != "" ]; then
      echo "RESULT|$panel_name|$error_state|$content"
      return 0
    fi
    retries=$((retries+1))
    sleep 1
  done
  echo "RESULT|$panel_name|FAIL|Could not verify after retries"
}

echo "Starting verification at $(date)" >> "$LOG"

# Start server initially
start_server
sleep 2

# Open browser to dashboard
export AGENT_BROWSER_SESSION="w30-5-verify-panels"
agent-browser open "http://localhost:3000/" 2>>"$LOG" >>"$LOG"
sleep 3

# Verify each panel
for panel in "${PANELS[@]}"; do
  echo "Verifying: $panel"
  verify_panel "$panel"
done

echo "Done at $(date)" >> "$LOG"
