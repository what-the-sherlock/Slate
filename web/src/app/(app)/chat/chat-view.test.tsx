import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { makeQueryClient } from "../../providers";
import { ChatView } from "./chat-view";

const SID = "11111111-1111-4111-8111-111111111111";
const replace = vi.fn();
const push = vi.fn();
let query = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push }),
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(query),
}));

function json(body: unknown, status = 200): Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

function sse(events: [string, unknown][]): Response {
  const body = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

const SOURCES = [
  { id: "s1", name: "octo/docs", connector: "github", data_class: "private", documents: 0, last_error: null, last_synced_at: null, status: "syncing", busy: true, healthy: true, visibility: "workspace" },
];

function mockApi(chat: (body: Record<string, unknown>) => Response) {
  const bodies: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.url;
      const path = new URL(url, "http://localhost").pathname;
      if (path === "/api/v1/sources") return json(SOURCES);
      if (path === "/api/v1/chat/sessions") return json([]);
      if (path === "/api/v1/chat") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        bodies.push(body);
        return chat(body);
      }
      return json({ type: "/problems/not-found", title: "Not found", status: 404 }, 404);
    }),
  );
  return bodies;
}

function renderView() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <TooltipProvider>
        <ChatView />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  query = "";
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
});

describe("ChatView", () => {
  it("explains a syncing repo instead of showing 0 documents", async () => {
    mockApi(() => sse([]));
    renderView();
    expect(await screen.findByText(/is still syncing/)).toBeInTheDocument();
    expect(screen.getByText("What do you want to know?")).toBeInTheDocument();
  });

  it("asks, streams the answer, labels a general answer and moves to the new chat's URL", async () => {
    const bodies = mockApi(() =>
      sse([
        ["meta", { v: 1, session_id: SID, user_message_id: "u1" }],
        ["token", { v: 1, text: "OAuth is an " }],
        ["token", { v: 1, text: "authorization framework." }],
        ["done", { v: 1, message_id: "a1", abstained: false, model: "gpt-x", latency_ms: 900, citations: [], closest: [], notices: [], mode: "general" }],
      ]),
    );
    renderView();
    const box = await screen.findByLabelText("Ask a question");
    fireEvent.change(box, { target: { value: "What is OAuth?" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(await screen.findByText("OAuth is an authorization framework.")).toBeInTheDocument();
    expect(screen.getByText("General answer · not from your sources")).toBeInTheDocument();
    expect(bodies[0]).toMatchObject({ question: "What is OAuth?", session_id: null });
    expect(replace).toHaveBeenCalledWith(`/chat?c=${SID}`, { scroll: false });
    expect(box).toHaveValue("");
  });

  it("a refused question goes back into the composer with the reason", async () => {
    mockApi(() => json({ type: "/problems/rate-limited", title: "Too many requests", status: 429, detail: "You're asking quickly. Wait a moment and try again." }, 429));
    renderView();
    const box = await screen.findByLabelText("Ask a question");
    fireEvent.change(box, { target: { value: "How much leave?" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    await waitFor(() => expect(box).toHaveValue("How much leave?"));
    expect(screen.getByRole("alert")).toHaveTextContent("You're asking quickly");
    expect(screen.getByText("What do you want to know?")).toBeInTheDocument(); // nothing half-added
  });
});

it("a stale or foreign chat link says so instead of showing an empty chat", async () => {
  const STALE = "33333333-3333-4333-8333-333333333333";
  query = `c=${STALE}`;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string) => {
      const path = new URL(typeof input === "string" ? input : input.url, "http://localhost").pathname;
      if (path === `/api/v1/chat/sessions/${STALE}/messages`) return json([]);
      if (path === "/api/v1/sources" || path === "/api/v1/chat/sessions") return json([]);
      return json({ type: "/problems/not-found", title: "Not found", status: 404 }, 404);
    }),
  );
  renderView();
  expect(await screen.findByText("This chat isn't available")).toBeInTheDocument();
});
