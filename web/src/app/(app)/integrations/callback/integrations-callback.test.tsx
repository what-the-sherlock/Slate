import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { savePending } from "@/lib/integrations";
import { IntegrationsCallback } from "./integrations-callback";

const replace = vi.fn();
const toastSuccess = vi.fn();
vi.mock("sonner", () => ({ toast: { success: (m: string) => toastSuccess(m), error: vi.fn() } }));
let search = "code=abc&state=s.t";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(search),
}));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

function mockApi(response: () => Response) {
  const bodies: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const raw = await req.clone().text();
      const path = new URL(req.url).pathname;
      bodies.push({ path, body: raw ? JSON.parse(raw) : null });
      if (path.endsWith("/sync")) return json({ source_id: "s1", status: "done" });
      return response();
    }),
  );
  return bodies;
}

function renderPage() {
  render(
    <StrictMode>
      <QueryClientProvider client={makeQueryClient()}>
        <IntegrationsCallback />
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  search = "code=abc&state=s.t";
  replace.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

describe("IntegrationsCallback", () => {
  it("sends the code once in a POST body, strips it from the URL, and returns to Integrations", async () => {
    savePending("linear");
    const calls = mockApi(() =>
      json({ id: "c1", account_label: "Kestrel", status: "connected", last_error: null, connected_at: "2026-11-27T10:00:00Z" }),
    );
    renderPage();
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/integrations"));
    expect(calls).toEqual([
      { path: "/api/v1/integrations/linear/callback", body: { code: "abc", state: "s.t" } },
      // then the first sync, right away, so the new account isn't empty for an hour
      { path: "/api/v1/integrations/connections/c1/sync", body: null },
    ]);
    expect(toastSuccess).toHaveBeenCalledWith("Connected and synced Kestrel.");
    expect(replace).toHaveBeenNthCalledWith(1, "/integrations/callback", { scroll: false });
  });

  it("shows the server's reason when the sign-in can't finish", async () => {
    savePending("linear");
    mockApi(() =>
      json({ type: "/problems/validation-failed", title: "Validation failed", status: 422, detail: "This sign-in link has expired or isn't yours." }, 422),
    );
    renderPage();
    expect(await screen.findByText("Couldn't finish connecting")).toBeInTheDocument();
    expect(screen.getByText(/expired or isn't yours/)).toBeInTheDocument();
  });

  it("treats a cancel at the provider plainly and calls nothing", () => {
    search = "error=access_denied";
    savePending("linear");
    const calls = mockApi(() => json({}));
    renderPage();
    expect(screen.getByText("Nothing was connected")).toBeInTheDocument();
    expect(calls).toEqual([]);
  });

  it("refuses a redirect it didn't start", () => {
    const calls = mockApi(() => json({}));
    renderPage(); // no pending provider in this tab
    expect(screen.getByText("This sign-in link has expired")).toBeInTheDocument();
    expect(calls).toEqual([]);
  });
});
