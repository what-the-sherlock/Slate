import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceOut } from "@/lib/chat";
import { CitationCard } from "./citation-card";

const doc = vi.hoisted(() => ({ uri: "https://github.com/acme/wave/issues/7", source_updated_at: null as string | null }));

vi.mock("@/lib/api/hooks", () => ({
  useDocument: (id: string | null) => ({ data: id ? doc : undefined }),
}));

const SOURCE: SourceOut = {
  n: 2,
  chunk_id: 7,
  document_id: "00000000-0000-0000-0000-000000000002",
  ordinal: 0,
  title: "Issue #7: Labels cut off",
  heading_path: "acme/wave",
  snippet: "We render at 300 DPI but scale as if 203 DPI.",
  connector: "github",
  updated_at: "2026-11-20T10:00:00Z",
  author: "mei-chen",
};

function open(source: SourceOut = SOURCE) {
  render(
    <CitationCard source={source}>
      <button type="button">2</button>
    </CitationCard>,
  );
  fireEvent.focus(screen.getByRole("button", { name: "2" })); // focus opens it, like hover
}

describe("CitationCard", () => {
  beforeEach(() => {
    doc.uri = "https://github.com/acme/wave/issues/7";
  });

  it("shows the source, the cited passage, its author and the original link", async () => {
    open();
    expect(await screen.findByText("Issue #7: Labels cut off")).toBeInTheDocument();
    expect(screen.getByText(/300 DPI/)).toBeInTheDocument();
    expect(screen.getByText(/mei-chen/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open source/ })).toHaveAttribute("href", doc.uri);
  });

  it("never links to a non-web location such as an upload", async () => {
    doc.uri = "upload://handbook.pdf";
    open();
    await screen.findByText("Issue #7: Labels cut off");
    expect(screen.queryByRole("link", { name: /Open source/ })).not.toBeInTheDocument();
  });
});
