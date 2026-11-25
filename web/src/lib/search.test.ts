import { describe, expect, it } from "vitest";
import {
  highlight,
  loadRecent,
  parseSearchState,
  pushRecent,
  queryTerms,
  snippet,
  toSearchParams,
  updatedAfter,
} from "./search";

const ID = "3f2b8c1e-1d2a-4c3b-9a8f-0e1d2c3b4a59";

describe("URL state", () => {
  it("round-trips and validates untrusted params", () => {
    const state = parseSearchState(new URLSearchParams(`q=leave&source=${ID}&source=nope&date=week`));
    expect(state).toEqual({ q: "leave", sources: [ID], date: "week" });
    expect(toSearchParams(state).toString()).toBe(`q=leave&source=${ID}&date=week`);
    expect(parseSearchState(new URLSearchParams("date=forever")).date).toBe("any");
    expect(parseSearchState(new URLSearchParams(`q=${"x".repeat(900)}`)).q).toHaveLength(500);
  });

  it("maps date presets to updated_after", () => {
    const now = new Date("2026-11-25T00:00:00Z");
    expect(updatedAfter("any", now)).toBeUndefined();
    expect(updatedAfter("week", now)).toBe("2026-11-18T00:00:00.000Z");
  });
});

describe("highlighting", () => {
  it("drops operators and short words", () => {
    expect(queryTerms('"paid leave" -draft OR a')).toEqual(["leave", "draft", "paid"]);
  });

  it("marks terms case-insensitively and keeps the rest as plain text", () => {
    expect(highlight("Paid leave is paid.", ["paid"])).toEqual([
      { text: "Paid", mark: true },
      { text: " leave is ", mark: false },
      { text: "paid", mark: true },
      { text: ".", mark: false },
    ]);
  });

  it("treats regex characters in queries literally", () => {
    expect(highlight("a+b (c)", ["a+b"])[0]).toEqual({ text: "a+b", mark: true });
    expect(() => highlight("x", ["(", "["])).not.toThrow();
  });
});

describe("snippet", () => {
  it("centres a window on the first match", () => {
    const text = `${"filler ".repeat(80)}the ERR_BUDGET code ${"tail ".repeat(80)}`;
    const s = snippet(text, ["err_budget"], 100);
    expect(s.startsWith("…")).toBe(true);
    expect(s.endsWith("…")).toBe(true);
    expect(s.toLowerCase()).toContain("err_budget");
  });

  it("returns short text whole", () => {
    expect(snippet("  short\n text ", ["x"])).toBe("short text");
  });
});

describe("recent searches", () => {
  function memory() {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
  }

  it("keeps the last five, newest first, without duplicates", () => {
    const s = memory();
    for (const q of ["a", "b", "c", "d", "e", "f", "c"]) pushRecent(q, s);
    expect(loadRecent(s)).toEqual(["c", "f", "e", "d", "b"]);
  });

  it("survives broken storage", () => {
    const broken = {
      getItem: () => "{not json",
      setItem: () => {
        throw new Error("quota");
      },
    };
    expect(loadRecent(broken)).toEqual([]);
    expect(pushRecent("x", broken)).toEqual(["x"]);
  });
});
