import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SourceOut } from "@/lib/chat";
import { CompactLinks, shortUrl } from "./sources-panel";
import { stripSources } from "./turn";

const src = (n: number): SourceOut => ({ n, chunk_id: n, document_id: `d${n}`, ordinal: 0, title: `T${n}`, heading_path: null, snippet: "" });

describe("stripSources", () => {
  it("puts cited sources first and collapses the rest into a count", () => {
    const { shown, hidden } = stripSources([1, 2, 3, 4, 5, 6].map(src), [5, 2]);
    expect(shown.map((s) => s.n)).toEqual([2, 5, 1]);
    expect(hidden).toBe(3);
  });

  it("shows everything when it fits", () => {
    const { shown, hidden } = stripSources([1, 2, 3, 4].map(src), []);
    expect(shown).toHaveLength(4);
    expect(hidden).toBe(0);
  });
});

describe("shortUrl", () => {
  it("keeps the host and drops long paths", () => {
    expect(shortUrl("https://www.naclind.com/investor-relations/announcements/")).toBe("naclind.com/…");
    expect(shortUrl("https://bseindia.com")).toBe("bseindia.com");
  });
});

describe("CompactLinks", () => {
  it("collapses bracketed tracking links and keeps surrounding text", () => {
    render(
      <p>
        <CompactLinks text={"at www.cdsl.com [https://mail.m3c.in/campaigns/abc/track-url/xyz]. Done"} />
      </p>,
    );
    const link = screen.getByRole("link", { name: "mail.m3c.in/…" });
    expect(link).toHaveAttribute("href", "https://mail.m3c.in/campaigns/abc/track-url/xyz");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(link.parentElement).toHaveTextContent("at www.cdsl.com mail.m3c.in/…. Done");
  });
});
