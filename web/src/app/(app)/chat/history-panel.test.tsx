import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { makeQueryClient } from "../../providers";
import { HistoryPanel, RenameField } from "./history-panel";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(""),
}));

const toastCalls: { message: string; action?: { label: string; onClick: () => void } }[] = [];
vi.mock("sonner", () => ({
  toast: Object.assign(
    (message: string, opts?: { action?: { label: string; onClick: () => void } }) => toastCalls.push({ message, action: opts?.action }),
    { error: (message: string) => toastCalls.push({ message: `error: ${message}` }) },
  ),
}));

const A = { id: "11111111-1111-4111-8111-111111111111", title: "I have sent an email today", created_at: new Date().toISOString() };
const B = { id: "22222222-2222-4222-8222-222222222222", title: "hello, what can you do", created_at: new Date().toISOString() };

function json(body: unknown, status = 200): Response {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": type } });
}

let calls: { method: string; path: string; body?: unknown }[] = [];
let sessions = [A, B];

beforeEach(() => {
  calls = [];
  toastCalls.length = 0;
  sessions = [A, B];
  push.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) => {
      const req = typeof input === "string" ? new Request(new URL(input, "http://localhost"), init) : input;
      const path = new URL(req.url).pathname;
      const body = req.method === "PATCH" ? await req.json() : undefined;
      calls.push({ method: req.method, path, body });
      if (req.method === "GET" && path === "/api/v1/chat/sessions") return json(sessions);
      if (req.method === "PATCH") return json({ ...A, title: (body as { title: string }).title.trim() });
      if (req.method === "DELETE" || path.endsWith("/restore")) return json(null, 204);
      return json({ type: "/problems/not-found", title: "Not found", status: 404 }, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

function renderPanel(activeId: string | null = null, onNewChat = vi.fn(), streaming: string[] = []) {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <TooltipProvider>
        <HistoryPanel activeId={activeId} streaming={streaming} onNewChat={onNewChat} onClose={vi.fn()} variant="column" />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return onNewChat;
}

async function openMenu(title: string) {
  fireEvent.click(await screen.findByRole("button", { name: `Options for ${title}` }));
  return screen.getByRole("menu", { name: `Options for ${title}` });
}

describe("chat history menu (ADR-0044)", () => {
  it("deletes at once, offers Undo, and Undo restores", async () => {
    renderPanel();
    const menu = await openMenu(A.title);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Delete/ }));
    await waitFor(() => expect(screen.queryByText(A.title)).not.toBeInTheDocument());
    expect(calls.some((c) => c.method === "DELETE" && c.path.endsWith(A.id))).toBe(true);
    await waitFor(() => expect(toastCalls.at(-1)?.message).toBe("Chat deleted"));
    toastCalls.at(-1)?.action?.onClick();
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith(`${A.id}/restore`))).toBe(true));
  });

  it("leaves a deleted open chat for a new one, and Undo returns to it", async () => {
    const onNewChat = renderPanel(A.id);
    const menu = await openMenu(A.title);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Delete/ }));
    await waitFor(() => expect(onNewChat).toHaveBeenCalled());
    toastCalls.at(-1)?.action?.onClick();
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/chat?c=${A.id}`, { scroll: false }));
  });

  it("can't delete a chat while it is still answering", async () => {
    renderPanel(null, vi.fn(), [A.id]);
    const menu = await openMenu(A.title);
    expect(within(menu).getByRole("menuitem", { name: /Delete/ })).toBeDisabled();
  });

  it("renames in place and saves the trimmed title", async () => {
    renderPanel();
    const menu = await openMenu(A.title);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Rename/ }));
    const field = screen.getByRole("textbox", { name: "Chat name" });
    fireEvent.change(field, { target: { value: "  Email to Aashutosh  " } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("Email to Aashutosh")).toBeInTheDocument());
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ title: "Email to Aashutosh" });
  });
});

describe("RenameField", () => {
  it("Escape cancels without saving, even when focus then leaves", () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    render(<RenameField initial="Old" onSave={onSave} onCancel={onCancel} />);
    const field = screen.getByRole("textbox", { name: "Chat name" });
    fireEvent.keyDown(field, { key: "Escape" });
    fireEvent.blur(field);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("leaving the field saves once", () => {
    const onSave = vi.fn();
    render(<RenameField initial="Old" onSave={onSave} onCancel={vi.fn()} />);
    const field = screen.getByRole("textbox", { name: "Chat name" });
    fireEvent.change(field, { target: { value: "New" } });
    fireEvent.blur(field);
    fireEvent.blur(field);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith("New");
  });
});
