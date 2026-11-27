import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import type { Integration } from "@/lib/integrations";
import { IntegrationsView } from "./integrations-view";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/integrations",
}));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": type } });
}

const base: Omit<Integration, "id" | "name" | "tagline" | "state"> = {
  auth: "oauth2",
  mode: "indexed",
  connect_path: null,
  notice: null,
  connections: [],
  sources: 0,
};
const CONNECTION = { id: "c1", account_label: "Kestrel Freight", status: "connected" as const, last_error: null, connected_at: "2026-11-27T10:00:00Z" };
const LIST: Integration[] = [
  { ...base, id: "github", name: "GitHub", tagline: "Docs and issues", auth: "github_app", state: "connected", connect_path: "/sources?add=github", sources: 2 },
  { ...base, id: "notion", name: "Notion", tagline: "Pages and databases", state: "connected", connections: [CONNECTION] },
  { ...base, id: "linear", name: "Linear", tagline: "Issues and projects", state: "not_connected" },
  { ...base, id: "gmail", name: "Gmail", tagline: "Your mail", state: "needs_reconnect", notice: "Google will show an unverified app notice.", connections: [{ ...CONNECTION, id: "c2", status: "needs_reconnect" }] },
  { ...base, id: "slack", name: "Slack", tagline: "Messages", state: "coming_soon" },
  { ...base, id: "hubspot", name: "HubSpot", tagline: "CRM", mode: "live", state: "not_set_up" },
];

let role = "member";
function mockApi() {
  const calls: { method: string; path: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      calls.push({ method: req.method, path });
      if (path === "/api/v1/integrations") return json(LIST);
      if (path === "/api/v1/me") return json({ role, user_id: "u1", workspace_id: "w1", email: "a@b.c", name: "A" });
      if (path.endsWith("/authorize")) return json({ authorize_url: "https://linear.app/oauth/authorize?state=s" });
      if (path.startsWith("/api/v1/integrations/connections/")) return json(null, 204);
      return json({}, 404);
    }),
  );
  return calls;
}

function renderPage(go = vi.fn()) {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <IntegrationsView go={go} />
    </QueryClientProvider>,
  );
  return go;
}

beforeEach(() => {
  role = "member";
  sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

function row(name: string): HTMLElement {
  return screen.getByText(name).closest("li")!;
}

describe("IntegrationsView", () => {
  it("shows every provider with an honest state and the account it's signed in as", async () => {
    mockApi();
    renderPage();
    await screen.findByText("Notion");
    expect(within(row("Notion")).getByText("Kestrel Freight")).toBeInTheDocument();
    expect(within(row("Gmail")).getByText("Needs sign-in")).toBeInTheDocument();
    expect(within(row("Slack")).getByText("Coming soon")).toBeInTheDocument();
    expect(within(row("HubSpot")).getByText("Not set up")).toBeInTheDocument();
    expect(within(row("GitHub")).getByText("2 sources")).toBeInTheDocument();
    expect(within(row("Slack")).queryByRole("button")).toBeNull();
  });

  it("filters by tab and search", async () => {
    mockApi();
    renderPage();
    await screen.findByText("Linear");
    fireEvent.click(screen.getByRole("tab", { name: /Needs sign-in/ }));
    expect(screen.queryByText("Linear")).toBeNull();
    expect(screen.getByText("Gmail")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /All/ }));
    fireEvent.change(screen.getByPlaceholderText("Search integrations"), { target: { value: "crm" } });
    expect(screen.getByText("HubSpot")).toBeInTheDocument();
    expect(screen.queryByText("Notion")).toBeNull();
  });

  it("connects by sending the browser to the provider and remembering which one", async () => {
    const calls = mockApi();
    const go = renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Connect Linear" }));
    await vi.waitFor(() => expect(go).toHaveBeenCalledWith("https://linear.app/oauth/authorize?state=s"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/integrations/linear/authorize")).toBe(true);
    expect(sessionStorage.getItem("slate.integrations.pending")).toContain("linear");
  });

  it("explains a provider's notice before leaving, and reconnects from it", async () => {
    mockApi();
    const go = renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Reconnect Gmail" }));
    expect(await screen.findByText("Google will show an unverified app notice.")).toBeInTheDocument();
    expect(go).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continue to sign in" }));
    await vi.waitFor(() => expect(go).toHaveBeenCalled());
  });

  it("disconnects after confirming, naming the account", async () => {
    const calls = mockApi();
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect Notion" }));
    expect(await screen.findByText(/deletes the stored sign-in for Kestrel Freight/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await vi.waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/v1/integrations/connections/c1")).toBe(true),
    );
  });

  it("links the older flows to their own setup", async () => {
    mockApi();
    renderPage();
    const link = await screen.findByRole("link", { name: "Add more GitHub" });
    expect(link).toHaveAttribute("href", "/sources?add=github");
  });

  it("gives viewers no connect actions", async () => {
    role = "viewer";
    mockApi();
    renderPage();
    await screen.findByText("Notion");
    await vi.waitFor(() => expect(screen.queryByRole("button", { name: /Connect Linear/ })).toBeNull());
  });
});
