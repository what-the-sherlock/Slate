import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { DiscordCallback } from "./discord-callback";

const G = "200000000000000000";
const replace = vi.fn();
const push = vi.fn();
let search = `code=abc123&state=st&guild_id=${G}`;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push }),
  useSearchParams: () => new URLSearchParams(search),
}));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

const SERVER = {
  id: G,
  name: "Acme",
  icon_url: null,
  channels: [
    { id: "1".repeat(18), name: "general", kind: "text", category: "Team", private: false, selectable: true, reason: null, connected: false },
    {
      id: "2".repeat(18),
      name: "staff",
      kind: "text",
      category: "Team",
      private: true,
      selectable: false,
      reason: "You can't read this channel",
      connected: false,
    },
    { id: "3".repeat(18), name: "help", kind: "forum", category: null, private: false, selectable: true, reason: null, connected: true },
  ],
};

function mockApi(overrides: Record<string, () => Response> = {}) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      calls.push(path);
      if (overrides[path]) return overrides[path]();
      if (path === "/api/v1/sources/discord/installations") return json({ grant: "G" });
      if (path === "/api/v1/sources/discord/channels") return json(SERVER);
      if (path === "/api/v1/sources/discord/connect") return json({ source_id: "s1", name: "Acme", job_id: "j1", channels: 2 }, 201);
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
        <DiscordCallback />
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  search = `code=abc123&state=st&guild_id=${G}`;
});
afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
  push.mockReset();
});

describe("DiscordCallback", () => {
  it("verifies once, strips the code from the URL, and explains unpickable channels", async () => {
    const calls = mockApi();
    renderPage();
    expect(await screen.findByText("general")).toBeInTheDocument();
    expect(calls.filter((c) => c.endsWith("/installations"))).toHaveLength(1); // StrictMode double effect
    expect(replace).toHaveBeenCalledWith("/sources/discord/callback", { scroll: false });
    expect(screen.getByText("You can't read this channel")).toBeInTheDocument();
    expect(screen.getByText("Private")).toBeInTheDocument();
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.map((b) => b.disabled)).toEqual([false, true, true]);
  });

  it("connects the selected channel and shows the first sync", async () => {
    mockApi();
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: /general/ }));
    fireEvent.click(screen.getByRole("button", { name: /connect 1 channel/i }));
    expect(await screen.findByText("Synced")).toBeInTheDocument();
    expect(screen.getByText(/Acme · 2 channels/)).toBeInTheDocument();
  });

  it("shows a cancel from Discord plainly", () => {
    search = "error=access_denied&error_description=cancelled";
    mockApi();
    renderPage();
    expect(screen.getByText("Discord wasn't connected")).toBeInTheDocument();
  });

  it("refuses an unproven install with the server's reason", async () => {
    mockApi({
      "/api/v1/sources/discord/installations": () =>
        json({ type: "x", title: "Forbidden", status: 403, detail: "Discord didn't confirm you added Slate to this server." }, 403),
    });
    renderPage();
    expect(await screen.findByText(/didn't confirm you added Slate/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start again" })).toBeInTheDocument();
  });
});
