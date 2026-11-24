import { describe, expect, it } from "vitest";
import { ApiProblem, toApiProblem, unwrap } from "./client";

describe("toApiProblem", () => {
  it("parses an RFC 9457 body", () => {
    const p = toApiProblem(
      { type: "/problems/not-found", title: "Not found", status: 404, request_id: "req_1" },
      404,
    );
    expect(p).toBeInstanceOf(ApiProblem);
    expect(p.status).toBe(404);
    expect(p.isClientError).toBe(true);
    expect(p.problem.request_id).toBe("req_1");
  });

  it("falls back safely for a non-problem body", () => {
    const p = toApiProblem("<html>Bad gateway</html>", 502);
    expect(p.status).toBe(502);
    expect(p.problem.type).toBe("/problems/unknown");
    expect(p.isClientError).toBe(false);
    expect(p.message).toContain("HTTP 502");
  });
});

describe("unwrap", () => {
  it("returns data", () => {
    expect(unwrap({ data: { ok: 1 }, response: new Response() })).toEqual({ ok: 1 });
  });

  it("throws ApiProblem on error", () => {
    const response = new Response(null, { status: 429 });
    expect(() =>
      unwrap({ error: { type: "/problems/rate-limited", title: "Too many", status: 429 }, response }),
    ).toThrow(ApiProblem);
  });
});
