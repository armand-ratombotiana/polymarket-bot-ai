# ──────────────────────────────────────────────────────────────────────────────
# Polymarket Bot — Frontend (Next.js 16 + Bun)
# Multi-stage build to minimize final image size.
# Final image runs as non-root `nextjs` user.
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Dependencies ─────────────────────────────────────────────────────
# Install full deps (including devDependencies) needed for the build.
FROM oven/bun:1 AS deps
WORKDIR /app

# Copy lockfile + manifest first to maximise layer caching.
COPY package.json bun.lock ./
# If bun.lockb exists in legacy form, fall back to it (no-op when absent).
# Bun 1.x uses `bun.lock` (text); older repos may have `bun.lockb` (binary).
COPY bun.lockb* ./

RUN bun install --frozen-lockfile

# ── Stage 2: Builder ──────────────────────────────────────────────────────────
# Compile the Next.js standalone bundle.
FROM oven/bun:1 AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build Next.js (produces .next/standalone + .next/static).
# `output: "standalone"` is set in next.config.ts.
RUN bun run build

# ── Stage 3: Runner (minimal runtime image) ──────────────────────────────────
FROM oven/bun:1-slim AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# Standalone Next.js server reads PORT + HOSTNAME env vars.
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Create non-root user/group for least-privilege execution.
RUN addgroup --system --gid 1001 nodejs \
 && adduser  --system --uid 1001 nextjs

# Copy only the artifacts the standalone server needs at runtime.
# `public/` is served by the standalone server for static assets.
COPY --from=builder --chown=nextjs:nodejs /app/public          ./public
# Standalone server bundle (server.js + node_modules subset).
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
# Pre-built static chunks (CSS/JS) referenced by standalone server.
COPY --from=builder --chown=nextjs:nodejs /app/.next/static     ./.next/static

USER nextjs

EXPOSE 3000

# Bun can run the Node-style standalone server.js directly.
CMD ["bun", "server.js"]
