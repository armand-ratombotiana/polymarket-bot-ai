# W12-4 — full-stack-developer — Bundle analyzer + Next.js build optimization

## Task scope
Add `@next/bundle-analyzer`, wrap `next.config.ts` with it, add `webpack` `splitChunks` production optimization, add `analyze`/`analyze:server` npm scripts, refresh `scripts/analyze-bundle.sh`, create `.bundle-budget.json`, and write `docs/BUILD_OPTIMIZATION.md`.

## Work log
1. Read prior context:
   - `worklog.md` (last 150 lines) — confirmed this is a Next.js 16 trading-workstation dashboard with extensive lazy-loaded panels already in place from W8-10 → W9-6.
   - `next.config.ts` — minimal config (`output: "standalone"`, `typescript.ignoreBuildErrors: true`, `reactStrictMode: false`).
   - `package.json` — has `bun run build`, `bun run start`, `bun run lint` scripts; no analyzer dependency present.
   - `scripts/analyze-bundle.sh` — pre-existing W9-6 helper that runs `bun run build | grep "Route|First Load|├|└|┌"` (basic, no analyzer, no large-chunk finder, no total-size output).
   - `src/app/page.tsx` — confirmed all 10 panels already loaded via `lazyPanel()` helper which uses `next/dynamic` with `ssr: false` + `loading: () => <PanelLoadingSkeleton/>`. No additional work needed for Step 6 (verify lazy loading) — already complete.
2. Installed `@next/bundle-analyzer` (dev dep): `bun add -d @next/bundle-analyzer` → resolved `@next/bundle-analyzer@16.3.4`, updated `bun.lock`.
3. Rewrote `next.config.ts`:
   - Wrapped the existing `nextConfig` object with `withBundleAnalyzer(nextConfig)`.
   - Analyzer `enabled: process.env.ANALYZE === "true"` — opt-in only, no impact on `bun run dev` / `bun run build` startup.
   - `openAnalyzer: false` so the build doesn't spawn a browser in headless sandbox environments.
   - Added `webpack(config, { dev, isServer })` hook with production-client-only `splitChunks` (vendor cacheGroup with `maxSize: 244000`, common cacheGroup with `minChunks: 2`).
   - Preserved all existing settings (`output: "standalone"`, `typescript.ignoreBuildErrors`, `reactStrictMode: false`).
4. Added npm scripts to `package.json`:
   - `"analyze": "ANALYZE=true bun run build"`
   - `"analyze:server": "ANALYZE=true bun run build && bun run start"`
5. Rewrote `scripts/analyze-bundle.sh` to the W12-4 spec:
   - Runs `ANALYZE=true bun run build`, tees to `/tmp/build-output.log`.
   - Prints 4 sections: Route Summary, Large Chunks (>100KB), Total Bundle Size, Analyzer Reports.
   - Made executable (`chmod +x`).
6. Created `.bundle-budget.json`:
   - `max_first_load_kb: 350`, `max_per_chunk_kb: 244`, `max_total_static_kb: 2000`, `routes: {"/": 350, "/_app": 100}`, `warnings: [recharts, framer-motion]`.
   - Added a `$schema` placeholder + `_comment` field explaining the budget is human-enforced today.
7. Created `docs/BUILD_OPTIMIZATION.md` (8 sections):
   - §1 How to analyze (`bun run analyze`, `./scripts/analyze-bundle.sh`, sandbox caveat).
   - §2 Current bundle sizes (target table, build budget table).
   - §3 Optimization strategies applied (lazyPanel + ssr:false, splitChunks config, standalone output, tree-shaking-friendly imports).
   - §4 Bundle budget (file reference + enforcement plan).
   - §5 Tree-shaking verification (treemap inspection workflow + footguns).
   - §6 Code splitting strategy (route-level + panel-level + vendor-level; when to add a new dynamic import).
   - §7 Dependency audit (commands + heavy-dep watch list with sizes).
   - §8 Reference (links to relevant files + Next.js docs).
8. Verified lint: `bunx eslint next.config.ts` → clean (no output). Full `bun run lint` shows 2 pre-existing errors in `src/components/OfflineIndicator.stories.tsx` (a file added by the parallel W12-7 Storybook task — untracked, not touched by W12-4). My changes introduce **zero** new lint errors.
9. Did NOT run `bun run analyze` (per task spec — sandbox may OOM). Verified `next.config.ts` syntax by re-reading the file after writing + running eslint on it.

## Stage summary
- Installed `@next/bundle-analyzer@16.3.4` (dev dependency): ✅
- Wrapped `next.config.ts` with `withBundleAnalyzer(nextConfig)`: ✅
- Added `webpack` `splitChunks` production-client optimization (vendor `maxSize: 244000`, common `minChunks: 2`): ✅
- Added `analyze` + `analyze:server` npm scripts: ✅
- Updated `scripts/analyze-bundle.sh` (4-section output: route table, large chunks, total size, analyzer report pointers): ✅
- Verified `src/app/page.tsx` lazy panels already use `ssr: false` + `loading:` skeleton (via `lazyPanel` helper) — no changes needed: ✅
- Created `.bundle-budget.json` (350/244/2000 KB budget + per-route + warnings): ✅
- Created `docs/BUILD_OPTIMIZATION.md` (8 sections, ~280 lines): ✅
- Lint clean for all W12-4-touched files (`next.config.ts` lint = no output): ✅ (2 pre-existing errors in `OfflineIndicator.stories.tsx` from parallel W12-7 task are out of scope)
- Files modified: 3 (`next.config.ts`, `package.json`, `scripts/analyze-bundle.sh`)
- Files created: 2 (`.bundle-budget.json`, `docs/BUILD_OPTIMIZATION.md`)
- Files unchanged but verified: 1 (`src/app/page.tsx` — lazy panels already complete)
