import type { NextConfig } from "next";

// Where the FastAPI app lives. Local: `uv run python -m api.dev` on :8000.
// Vercel (later): the `api` project's URL, set as API_ORIGIN on the `web` project.
const apiOrigin = (process.env.API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  experimental: {
    // The rewrite proxy gives up after 30 s by default and answers with a bare 500.
    // /uploads/complete may process a small file inline for up to 120 s
    // (core/services/inline.py INLINE_TIMEOUT_S), so wait a bit longer than that.
    proxyTimeout: 150_000,
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // Same-origin API (ADR-0010): the browser only ever talks to this site.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
