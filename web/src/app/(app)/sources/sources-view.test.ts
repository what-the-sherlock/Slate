import { describe, expect, it } from "vitest";
import { skippedCount } from "./sources-view";

describe("skippedCount", () => {
  it("adds up the counts in a skip summary", () => {
    expect(skippedCount("30 PDFs (not supported yet), 7 Sheets (not supported yet), 2 more")).toBe("39 skipped");
    expect(skippedCount("1 file (too large)")).toBe("1 skipped");
  });

  it("falls back when the summary has no counts", () => {
    expect(skippedCount("stopped at the per-sync limit")).toBe("Some skipped");
  });
});
