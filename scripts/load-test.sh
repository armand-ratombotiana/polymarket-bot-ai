#!/bin/bash
# Run load tests against the backend
# Usage: ./scripts/load-test.sh [users] [duration_seconds]
set -euo pipefail

USERS="${1:-20}"
DURATION="${2:-60}"
HOST="${LOCUST_HOST:-http://localhost:8080}"

echo "Running load test: $USERS users for ${DURATION}s against $HOST"

cd /home/z/my-project/mini-services/polymarket-bot
locust -f tests/load/locustfile.py \
  --host="$HOST" \
  --users="$USERS" \
  --spawn-rate=2 \
  --run-time="${DURATION}s" \
  --headless \
  --csv=load_test_results \
  --only-summary

echo ""
echo "Load test complete. Results in mini-services/polymarket-bot/load_test_results_*"
