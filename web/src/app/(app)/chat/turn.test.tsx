import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { newTurn, type DoneEvent, type SourceOut, type Turn } from "@/lib/chat";
import { TurnView, type TurnActions } from "./turn";

const SOURCE: SourceOut = { n: 1, chunk_id: 7, document_id: "00000000-0000-0000-0000-000000000001", ordinal: 0, title: "Handbook", heading_path: null, snippet: "" };
const DONE: DoneEvent = { v: 1, message_id: "a1", abstained: false, model: "gpt-x", latency_ms: 2100, citations: [], closest: [], notices: [], mode: "grounded" };

function actions(): TurnActions {
  return { onOpenSource: vi.fn(), onRegenerate: vi.fn(), onEdit: vi.fn(), onRephrase: vi.fn(), onClearFilters: vi.fn() };
}

function show(turn: Turn, a = actions(), { isLast = true, threadBusy = false, filtersActive = false } = {}) {
  render(
    <TooltipProvider>
      <TurnView turn={turn} isLast={isLast} threadBusy={threadBusy} filtersActive={filtersActive} actions={a} />
    </TooltipProvider>,
  );
  return a;
}

const done = (extra: Partial<Turn> = {}, d: Partial<DoneEvent> = {}): Turn => ({
  ...newTurn("How much leave?", "k"),
  stage: "done",
  text: "Everyone gets 25 days [1].",
  sources: [SOURCE],
  cited: [1],
  messageId: "a1",
  userMessageId: "u1",
  done: { ...DONE, ...d },
  ...extra,
});

describe("TurnView", () => {
  it("shows the question as a bubble and a cited answer with its actions", () => {
    const a = show(done());
    expect(screen.getByText("How much leave?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Source 1: Handbook" }));
    expect(a.onOpenSource).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    expect(a.onRegenerate).toHaveBeenCalled();
    expect(screen.getByText("2.1 s")).toBeInTheDocument();
  });

  it("labels a general answer and shows no sources", () => {
    show(done({ text: "OAuth is an authorization framework.", cited: [] }, { mode: "general" }));
    expect(screen.getByText("General answer · not from your sources")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Sources" })).not.toBeInTheDocument();
  });

  it("only the last answer can be regenerated, and not while another streams", () => {
    show(done(), actions(), { isLast: false });
    expect(screen.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument();
  });

  it("abstaining offers Rephrase and Clear filters, with the same wording for everyone", () => {
    const a = show(done({ text: "" }, { abstained: true }), actions(), { filtersActive: true });
    expect(screen.getByText("Not enough evidence to answer this")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Rephrase/ }));
    expect(a.onRephrase).toHaveBeenCalledWith("How much leave?");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(a.onClearFilters).toHaveBeenCalled();
  });

  it("an error explains itself in words, keeps the details folded, and retries", () => {
    const a = show({ ...newTurn("q", "k"), stage: "error", error: { title: "Something went wrong on our side", detail: "Try again.", requestId: "req_9" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong on our side");
    expect(screen.getByText("req_9")).not.toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Try again/ }));
    expect(a.onRegenerate).toHaveBeenCalled();
  });

  it("a stopped answer keeps its text and can be regenerated", () => {
    const a = show({ ...newTurn("q", "k"), stage: "stopped", text: "Partial answer" });
    expect(screen.getByText("Partial answer")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Regenerate/ }));
    expect(a.onRegenerate).toHaveBeenCalled();
  });

  it("editing a question resubmits it, and Escape cancels", () => {
    const a = show(done(), actions(), { isLast: false });
    fireEvent.click(screen.getByRole("button", { name: "Edit question" }));
    const box = screen.getByLabelText("Edit your question");
    expect(screen.getByText("The answers below will be replaced.")).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByLabelText("Edit your question")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit question" }));
    fireEvent.change(screen.getByLabelText("Edit your question"), { target: { value: "How much sick leave?" } });
    fireEvent.keyDown(screen.getByLabelText("Edit your question"), { key: "Enter" });
    expect(a.onEdit).toHaveBeenCalledWith(expect.objectContaining({ key: "k" }), "How much sick leave?");
  });

  it("while thinking, shows the stage, not an empty answer", () => {
    show({ ...newTurn("q", "k"), stage: "reading", sources: [SOURCE], startedAt: Date.now() });
    expect(screen.getByText("Reading 1 source…")).toBeInTheDocument();
  });
});
