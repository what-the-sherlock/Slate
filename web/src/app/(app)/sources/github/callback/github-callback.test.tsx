import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { GitHubCallback } from "./github-callback";

const replace = vi.fn();
const push = vi.fn();
let search = "installation_id=42&code=abc&state=st&setup_action=install";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push }),
  useSearchParams: () => new URLSearchParams(search),
}));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

const REPOS = [
  { id: 1, full_name: "octo/docs", private: true, description: "Docs", html_url: "u", connected: false },
  { id: 2, full_name: "octo/site", private: false, description: null, html_url: "u", connected: true },
];

function mockApi(overrides: Record<string, () => Response> = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      calls.push(path);
      if (overrides[path]) return overrides[path]();
      if (path === "/api/v1/sources/github/installations") return json({ grant: "G" });
      if (path === "/api/v1/sources/github/repos") return json(REPOS);
      if (path === "/api/v1/sources/github/connect") return json([{ source_id: "s1", name: "octo/docs", job_id: "j1" }], 201);
      if (path === "/api/v1/sources/s1/sync") return json({ job_id: "j1", status: "done" });
      return json({}, 404);
    }),
  );
  return calls;
}

function renderPage() {
  return render(
    <StrictMode>
      <QueryClientProvider client={makeQueryClient()}>
        <GitHubCallback />
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  search = "installation_id=42&code=abc&state=st&setup_action=install";
});
afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
  push.mockReset();
});

describe("GitHubCallback", () => {
  it("verifies once, strips the code from the URL, then lists repositories", async () => {
    const calls = mockApi();
    renderPage();
    expect(await screen.findByText("octo/docs")).toBeInTheDocument();
    expect(calls.filter((c) => c.endsWith("/installations"))).toHaveLength(1); // StrictMode double effect
    expect(replace).toHaveBeenCalledWith("/sources/github/callback", { scroll: false });
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect((screen.getAllByRole("checkbox")[1] as HTMLInputElement).disabled).toBe(true);
  });

  it("connects the selected repo and shows its first sync", async () => {
    mockApi();
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: /octo\/docs/ }));
    fireEvent.click(screen.getByRole("button", { name: /connect 1 repository/i }));
    expect(await screen.findByText("Synced")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /go to sources/i })).toHaveAttribute("href", "/sources");
  });

  it("explains an installation that isn't yours", async () => {
    mockApi({
      "/api/v1/sources/github/installations": () =>
        json({ type: "/problems/permission-denied", title: "Permission denied", status: 403, detail: "GitHub didn't confirm this installation is yours." }, 403),
    });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("didn't confirm this installation is yours");
    expect(screen.getByRole("link", { name: /start again/i })).toHaveAttribute("href", "/sources?add=1");
  });

  it("waits for an organisation owner's approval", () => {
    search = "setup_action=request";
    mockApi();
    renderPage();
    expect(screen.getByText("Waiting for approval")).toBeInTheDocument();
  });

  it("offers a way back when the link is incomplete", () => {
    search = "";
    mockApi();
    renderPage();
    expect(screen.getByRole("alert")).toHaveTextContent("already used");
  });

  it("guides the user when the app can see no repositories", async () => {
    mockApi({ "/api/v1/sources/github/repos": () => json([]) });
    renderPage();
    expect(await screen.findByText("The app can't see any repositories")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /choose repositories on github/i })).toHaveAttribute(
        "href",
        "https://github.com/settings/installations/42",
      ),
    );
  });
});
