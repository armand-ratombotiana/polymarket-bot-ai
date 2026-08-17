import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output — required by webui/Dockerfile (copies .next/standalone + server.js)
  output: "standalone",
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080",
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8080/ws",
  },
};

export default nextConfig;
