#!/bin/bash
# W12-4 — Bundle analysis helper.
#
# Runs a production build with @next/bundle-analyzer enabled
# (ANALYZE=true), then prints a summary of:
#   1. The route table Next.js prints at the end of a build
#      (Route | Size | First Load JS) — the most actionable single
#      number for catching per-route regressions.
#   2. Any chunk file in .next/static/chunks larger than 100KB
#      (sorted by size, largest first) — flags chunks that should
#      be split further or lazy-loaded.
#   3. Total size of .next/static/ — high-level size trend.
#
# Usage:
#   ./scripts/analyze-bundle.sh
#
# Output: stdout summary (see above). Full build log is written to
# /tmp/build-output.log. The analyzer's interactive treemap reports
# are written to .next/analyze/client.html and .next/analyze/server.html.
#
# Notes:
#   - This runs a full production build (slow — 1–2 min). Do NOT
#     invoke during normal dev work; use `bun run dev` for that.
#   - In CI, pipe to `tee bundle-report.txt` to keep a per-build
#     artifact.
#   - If the build OOMs in the sandbox, run `bun run analyze` in a
#     machine with more RAM and open the generated HTML reports in a
#     browser for per-module drill-down.
set -euo pipefail

echo "=== Bundle Analysis ==="
echo "Running production build with analyzer..."
cd /home/z/my-project

ANALYZE=true bun run build 2>&1 | tee /tmp/build-output.log

echo ""
echo "=== Route Summary ==="
grep -E "Route|First Load|├|└|┌|○|●|λ" /tmp/build-output.log | head -50

echo ""
echo "=== Large Chunks (>100KB) ==="
find .next/static/chunks -name "*.js" -size +100k -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20

echo ""
echo "=== Total Bundle Size ==="
du -sh .next/static/ 2>/dev/null || echo "Build may have failed"

echo ""
echo "=== Analyzer Reports ==="
echo "Client treemap: .next/analyze/client.html"
echo "Server treemap: .next/analyze/server.html"
echo "Open either file in a browser for per-module drill-down."
