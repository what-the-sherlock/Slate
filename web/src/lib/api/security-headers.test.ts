import { afterEach, describe, expect, it, vi } from "vitest";
import { api, readCsrfToken } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "slate_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

function captureFetch() {
  const seen: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      seen.push(req);
      return new Response(JSON.stringify({ status: "ok", version: "x" }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return seen;
}

describe("security headers", () => {
  it("reads the CSRF cookie in dev and __Host- forms", () => {
    expect(readCsrfToken("a=1; slate_csrf=abc%2B; b=2")).toBe("abc+");
    expect(readCsrfToken("__Host-slate_csrf=xyz")).toBe("xyz");
    expect(readCsrfToken("other=1")).toBe("");
  });

  it("sends X-Requested-With on every request but CSRF only on mutations", async () => {
    document.cookie = "slate_csrf=tok123; path=/";
    const seen = captureFetch();
    await api.GET("/api/v1/healthz");
    await api.POST("/api/v1/auth/logout");
    expect(seen[0]?.headers.get("x-requested-with")).toBe("slate");
    expect(seen[0]?.headers.get("x-csrf-token")).toBeNull();
    expect(seen[1]?.headers.get("x-csrf-token")).toBe("tok123");
  });
});
