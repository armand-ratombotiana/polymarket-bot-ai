import type { NextConfig } from "next";
import bundleAnalyzer from "@next/bundle-analyzer";

// W12-4 — Bundle analysis. The analyzer is opt-in: it only runs when
// ANALYZE=true is set in the environment (e.g. `bun run analyze`). In
// normal `bun run dev` / `bun run build` invocations the wrapper is a
// no-op pass-through, so production build time and dev-server startup
// are unaffected.
const withBundleAnalyzer = bundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
  openAnalyzer: false, // Don't auto-open a browser tab — the reports
  // are written to .next/analyze/ and can be opened manually. This
  // matters in headless / sandboxed environments where no browser is
  // attached (otherwise the spawned `open` call hangs the build).
});

const nextConfig: NextConfig = {
  output: "standalone",
  turbopack: {},  // Silence Turbopack warning — webpack config only runs for production builds
  /* config options here */
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,

  // W12-4 — Production client bundle optimization. Splits large
  // vendor code (react / recharts / framer-motion / @radix-ui) into
  // its own parallel chunks so the route-level First Load JS stays
  // under the budget declared in `.bundle-budget.json`. The
  // `vendor` cacheGroup pulls everything under node_modules into a
  // `vendors-*` chunk; `maxSize: 244000` (~244KB) lets webpack further
  // split vendor chunks that exceed the budget instead of emitting a
  // single multi-megabyte vendor blob. `common` re-uses any module
  // shared by ≥2 routes so a shared helper isn't duplicated across
  // route chunks. This block only runs for the client build (dev +
  // server builds keep Next.js defaults).
  webpack: (config, { dev, isServer }) => {
    if (!dev && !isServer) {
      config.optimization = {
        ...config.optimization,
        splitChunks: {
          chunks: "all",
          cacheGroups: {
            vendor: {
              test: /[\\/]node_modules[\\/]/,
              name: "vendors",
              chunks: "all",
              maxSize: 244000, // 244KB chunks
            },
            common: {
              name: "common",
              minChunks: 2,
              chunks: "all",
              reuseExistingChunk: true,
            },
          },
        },
      };
    }
    return config;
  },
};

export default withBundleAnalyzer(nextConfig);
