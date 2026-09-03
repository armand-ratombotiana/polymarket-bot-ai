#!/bin/bash
# W9-6 — bundle analysis helper.
#
# Runs `bun run build` and extracts the route table that Next.js prints at
# the end of a production build. The table shows the First Load JS size
# for each route, which is the most actionable single number for catching
# performance regressions (a +50KB jump on `/` means a panel got heavier
# or a new dep leaked into the initial bundle).
#
# Usage:
#   ./scripts/analyze-bundle.sh
#
# Output: up to 50 lines of the build's route table (Route | Size | First
# Load JS), printed to stdout. Exit code is `bun run build`'s exit code.
#
# Notes:
#   - This runs a full production build (slow). Do NOT run it during dev.
#   - The build output is filtered with grep so only the route table is
#     shown — the actual bundle is written to `.next/` and can be inspected
#     with `@next/bundle-analyzer` if more detail is needed.
#   - In CI, pipe this script's output to `tee bundle-report.txt` to keep
#     a per-build artifact.
set -euo pipefail

cd /home/z/my-project

echo "==> Running production build to capture bundle sizes…"
echo "    (this may take 1–2 minutes)"
echo ""

bun run build 2>&1 | grep -E "Route|First Load|├|└|┌" | head -50

echo ""
echo "==> Done."
echo "    For per-module drill-down, install @next/bundle-analyzer and"
echo "    re-run with ANALYZE=true bun run build."
