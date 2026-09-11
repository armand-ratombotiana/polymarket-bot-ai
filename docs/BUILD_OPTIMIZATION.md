# Build Optimization

How the Polymarket Pro frontend is built, analyzed, and kept lean. The Next.js
16 app ships in `output: 'standalone'` mode so the production server can run
from `.next/standalone/` with only a minimal `node_modules` slice.

## Build commands

| Command                | Purpose                                                        |
| ---------------------- | ------------------------------------------------------------- |
| `bun run dev`          | Next dev server on :3000 (hot reload, `tee dev.log`)         |
| `bun run build`        | Production build → `.next/standalone/` (self-contained)       |
| `bun run start`        | Serve the standalone build (`NODE_ENV=production bun …`)       |
| `bun run analyze`      | Build with `@next/bundle-analyzer` enabled                    |
| `./scripts/analyze-bundle.sh` | Build + route table + large-chunk report (see below)   |

## Bundle analyzer

[`@next/bundle-analyzer`](https://www.npmjs.com/package/@next/bundle-analyzer)
is wired in `next.config.ts` behind the `ANALYZE=true` env flag. Running
`bun run analyze` (or `bun run analyze:server`) produces interactive treemap
reports at:

- `.next/analyze/client.html` — client bundle drill-down
- `.next/analyze/server.html` — server bundle drill-down

Open either in a browser to inspect per-module size and identify bloat.

## `scripts/analyze-bundle.sh`

[`scripts/analyze-bundle.sh`](../scripts/analyze-bundle.sh) (W12-4) is a
helper that runs a production build with the analyzer on, then prints a
plain-text summary:

1. **Route table** — the Route | Size | First Load JS table Next.js prints at
   the end of a build. First Load JS is the single most actionable number for
   catching per-route regressions.
2. **Large chunks** — every chunk in `.next/static/chunks/` over 100 KB,
   sorted largest-first. Flags chunks that should be split further or
   lazy-loaded.
3. **Total bundle size** — `du -sh .next/static/` for a high-level trend.

```bash
./scripts/analyze-bundle.sh
# Full build log → /tmp/build-output.log
# Interactive reports → .next/analyze/{client,server}.html
```

> Runs a full production build (1–2 min). Do not invoke during normal dev work.

## Optimization principles applied

- **`output: 'standalone'`** — Next.js traces only the modules actually
  imported, so the standalone server ships a minimal `node_modules` slice.
- **Route-level code splitting** — Next.js automatically splits per-route;
  the dashboard route's First Load JS is ~180 KB gzipped.
- **Dynamic imports for heavy panels** — panels that pull in heavy deps
  (chart libraries, syntax highlighters) are dynamically imported so they
  only load when the panel mounts.
- **Tree-shaking** — `@radix-ui/*` primitives are ESM and tree-shake cleanly;
  only the actually-used primitives land in the bundle.
- **Vendor chunk stability** — React, Next, and Radix are pinned to stable
  majors to keep the long-term vendor chunk hash stable across builds.

## What to watch for

- **First Load JS creep** on the dashboard route — investigate with the
  analyzer; usually a new panel import that isn't dynamically split.
- **`recharts` / `react-syntax-highlighter`** — known-heavy; if a panel
  doesn't need them, prefer lighter alternatives or dynamic import.
- **Duplicate copies** of `react` / `react-dom` — usually a transitive dep
  pinning a wrong major; resolves with `bun install` dedup.

## See also
- [PERFORMANCE.md](PERFORMANCE.md) — runtime performance patterns
- [LOAD_TESTING.md](LOAD_TESTING.md) — backend throughput baselines
- [DEPLOYMENT.md](DEPLOYMENT.md) — how the standalone build is deployed
