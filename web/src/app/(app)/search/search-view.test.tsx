import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "../../providers";
import { SearchView } from "./search-view";

let query = "";
const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  usePathname: () => "/search",
  useSearchParams: () => new URLSearchParams(query),
}));

const DOC = "3f2b8c1e-1d2a-4c3b-9a8f-0e1d2c3b4a59";

function json(body: unknown, status = 200): Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

function mockApi(search: () => Response) {
  const calls: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/sources") return json([]);
      if (path === "/api/v1/search") {
        calls.push(await input.clone().json());
        return search();
      }
      if (path === `/api/v1/documents/${DOC}`)
        return json({
          id: DOC,
          source_id: DOC,
          source_name: "Uploads",
          title: "Handbook",
          uri: "upload://h",
          mime: "text/markdown",
          source_updated_at: null,
          chunks: [{ ordinal: 0, heading_path: "Leave", content: "Paid leave is <b>25</b> days." }],
        });
      throw new TypeError("Failed to fetch");
    }),
  );
  return calls;
}

function renderView() {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return render(
    <QueryClientProvider client={client}>
      <SearchView />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  query = "";
});

const HIT = {
  chunk_id: 1,
  document_id: DOC,
  title: "Handbook",
  heading_path: "Leave",
  content: "Paid leave is <b>25</b> days.",
  ordinal: 0,
  score: 0.03,
  matched_by: ["keyword", "vector"],
};

describe("SearchView", () => {
  it("shows recent searches and examples before searching, without calling the API", () => {
    const calls = mockApi(() => json({}));
    renderView();
    expect(screen.getByText("Recent searches")).toBeInTheDocument();
    expect(screen.getByText("expense approval limit")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("renders results with highlighted terms, as text, and a preview", async () => {
    query = "q=paid+leave&date=week";
    const calls = mockApi(() => json({ hits: [HIT], lanes: ["keyword", "vector"], notices: [] }));
    renderView();
    const list = await screen.findByRole("listbox", { name: "Search results" });
    const marks = within(list).getAllByText(/paid|leave/i, { selector: "mark" });
    expect(marks.map((m) => m.textContent)).toEqual(["Paid", "leave"]);
    expect(within(list).getAllByText(/<b>25<\/b>/).length).toBeGreaterThan(0); // never parsed as HTML
    expect(screen.getByRole("status")).toHaveTextContent("1 result");
    expect(await screen.findByText(/Uploads · 1 passages/)).toBeInTheDocument();
    expect(calls[0]).toMatchObject({ query: "paid leave", limit: 20, source_ids: [] });
    expect((calls[0] as { updated_after: string }).updated_after).toMatch(/^\d{4}-/);
  });

  it("shows degradation notices honestly", async () => {
    query = "q=leave";
    mockApi(() =>
      json({ hits: [HIT], lanes: ["keyword"], notices: ["Meaning-based search is unavailable right now; showing keyword matches."] }),
    );
    renderView();
    expect(await screen.findByText(/Meaning-based search is unavailable/)).toBeInTheDocument();
  });

  it("shows the empty state and an error with retry", async () => {
    query = "q=zzz";
    mockApi(() => json({ hits: [], lanes: ["keyword"], notices: [] }));
    const { unmount } = renderView();
    expect(await screen.findByText('No results for "zzz"')).toBeInTheDocument();
    unmount();

    mockApi(() => json({ type: "/problems/rate-limited", title: "Too many", status: 429, detail: "Too many searches." }, 429));
    renderView();
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many searches.");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
