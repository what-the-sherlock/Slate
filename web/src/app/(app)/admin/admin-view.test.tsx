import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { AdminView } from "./admin-view";
import { accountStatus, bucketLabel, niceMax, percent, weekDelta } from "./labels";

let search = new URLSearchParams();
const replace = vi.fn((url: string) => {
  search = new URLSearchParams(url.split("?")[1] ?? "");
});
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => search,
  usePathname: () => "/admin",
}));

const ME = { user: { id: "me", email: "admin@slate.test", name: "Admin" }, workspace_id: "w", role: "owner", email_verified: true };
const OVERVIEW = {
  total: 12, verified: 9, unverified: 2, disabled: 1, new_today: 1, new_7d: 4, new_30d: 10,
  new_prev_7d: 2, active_7d: 5, active_30d: 8, failed_logins_24h: 3,
};
const USER = (id: string, extra: object = {}) => ({
  id, email: `${id}@acme.dev`, name: id.toUpperCase(), joined_at: "2026-11-01T10:00:00Z",
  email_verified: true, disabled: false, last_active_at: null, workspaces: 1, you: false, ...extra,
});

type Call = { path: string; method: string; query: URLSearchParams; body: unknown };
let calls: Call[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function mockApi({ admin = true }: { admin?: boolean } = {}) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const url = new URL(req.url);
      const body = req.method === "PATCH" ? await req.json() : null;
      calls.push({ path: url.pathname, method: req.method, query: url.searchParams, body });
      switch (url.pathname) {
        case "/api/v1/me":
          return json({ ...ME, is_platform_admin: admin });
        case "/api/v1/admin/overview":
          return admin ? json(OVERVIEW) : json({ status: 404, title: "Not found" }, 404);
        case "/api/v1/admin/signups":
          return json({ bucket: "day", points: [{ start: "2026-11-27", count: 2 }, { start: "2026-11-28", count: 1 }] });
        case "/api/v1/admin/users":
          return json({ items: [USER("bob"), USER("me", { you: true })], next_cursor: null, total: 2 });
        case "/api/v1/admin/users/bob":
          return json(USER("bob", { disabled: true }));
        default:
          return json({}, 404);
      }
    }),
  );
}

function renderView() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <AdminView />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  search = new URLSearchParams();
  replace.mockClear();
});

describe("AdminView", () => {
  it("shows a plain not-found page to anyone but the platform admin", async () => {
    mockApi({ admin: false });
    renderView();
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
    expect(screen.queryByText("Accounts")).not.toBeInTheDocument();
  });

  it("renders the overview figures", async () => {
    mockApi();
    renderView();
    expect(await screen.findAllByText("75%")).toHaveLength(2); // 9 of 12 verified: KPI + breakdown
    expect(screen.getByText("+2 vs last week")).toBeInTheDocument();
    const total = await screen.findByText(/sign-ups in this period/);
    expect(total.textContent).toBe("3 sign-ups in this period");
    fireEvent.click(screen.getByRole("button", { name: "Show table" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("asks before disabling, then sends the change", async () => {
    mockApi();
    search = new URLSearchParams("tab=users");
    renderView();
    expect(await screen.findByText("bob@acme.dev")).toBeInTheDocument();
    // You can't disable yourself: there's no button on your own row.
    expect(screen.queryByRole("button", { name: "Disable me@acme.dev" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disable bob@acme.dev" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/signed out everywhere/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Disable account" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ disabled: true });
  });

  it("sorts by a column header and filters by status", async () => {
    mockApi();
    search = new URLSearchParams("tab=users");
    renderView();
    await screen.findByText("bob@acme.dev");
    fireEvent.click(screen.getByRole("button", { name: /account/i }));
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "disabled" } });
    await waitFor(() => {
      const last = calls.filter((c) => c.path === "/api/v1/admin/users").at(-1);
      expect(last?.query.get("sort")).toBe("email");
      expect(last?.query.get("order")).toBe("asc");
      expect(last?.query.get("status")).toBe("disabled");
    });
  });
});

describe("admin labels", () => {
  it("picks one status per account, disabled first", () => {
    expect(accountStatus({ disabled: true, email_verified: false }).label).toBe("Disabled");
    expect(accountStatus({ disabled: false, email_verified: false }).label).toBe("Unverified");
    expect(accountStatus({ disabled: false, email_verified: true }).label).toBe("Verified");
  });

  it("formats deltas, percentages and axis maxima", () => {
    expect(weekDelta(3, 5)).toBe("−2 vs last week");
    expect(weekDelta(4, 4)).toBe("Same as last week");
    expect(percent(1, 0)).toBe("—");
    expect([niceMax(0), niceMax(7), niceMax(23), niceMax(100)]).toEqual([4, 10, 50, 100]);
  });

  it("reads bucket dates as UTC calendar days", () => {
    expect(bucketLabel("2026-11-01", "month", true)).toMatch(/November 2026/);
    expect(bucketLabel("2026-11-30", "week", true)).toMatch(/^Week of /);
  });
});
