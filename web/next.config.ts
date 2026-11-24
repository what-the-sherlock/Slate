import type { NextConfig } from "next";

// Where the FastAPI app lives. Local: `uv run python -m api.dev` on :8000.
// Vercel (later): the `api` project's URL, set as API_ORIGIN on the `web` project.
const apiOrigin = (process.env.API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
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
