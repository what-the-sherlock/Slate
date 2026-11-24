import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiProblem } from "@/lib/api/client";
import { makeQueryClient } from "../providers";
import { StatusPanel } from "./status-panel";

function json(body: unknown, status = 200): Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

function mockApi(routes: Record<string, () => Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string) => {
      const url = typeof input === "string" ? input : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const handler = routes[path];
      if (!handler) throw new TypeError("Failed to fetch");
      return handler();
    }),
  );
}

function renderPanel() {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return render(
    <QueryClientProvider client={client}>
      <StatusPanel />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("StatusPanel", () => {
  it("shows all systems OK", async () => {
    mockApi({
      "/api/v1/healthz": () => json({ status: "ok", version: "0.1.0" }),
      "/api/v1/readyz": () =>
        json({
          status: "ready",
          database: true,
          migrations_current: true,
          migration_revision: "0001",
          expected_revision: "0001",
        }),
    });
    renderPanel();
    expect(await screen.findByText("v0.1.0")).toBeInTheDocument();
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getAllByText("OK")).toHaveLength(3);
  });

  it("reads the 503 body when the database is down", async () => {
    mockApi({
      "/api/v1/healthz": () => json({ status: "ok", version: "0.1.0" }),
      "/api/v1/readyz": () =>
        json(
          {
            status: "not_ready",
            database: false,
            migrations_current: false,
            migration_revision: null,
            expected_revision: "0001",
          },
          503,
        ),
    });
    renderPanel();
    expect(await screen.findByText("Unreachable")).toBeInTheDocument();
    expect(screen.getAllByText("Down")).toHaveLength(2);
  });

  it("says when the API can't be reached", async () => {
    mockApi({});
    renderPanel();
    expect(await screen.findAllByText("Couldn't reach the API")).toHaveLength(3);
  });
});

describe("query retry policy", () => {
  it("does not retry 4xx problems but retries network errors", () => {
    const retry = makeQueryClient().getDefaultOptions().queries?.retry;
    if (typeof retry !== "function") throw new Error("retry should be a function");
    const notFound = new ApiProblem({ type: "/problems/not-found", title: "Not found", status: 404 });
    expect(retry(0, notFound)).toBe(false);
    expect(retry(0, new TypeError("Failed to fetch"))).toBe(true);
    expect(retry(2, new TypeError("Failed to fetch"))).toBe(false);
  });
});
