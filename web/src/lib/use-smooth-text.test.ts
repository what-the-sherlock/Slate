import { describe, expect, it } from "vitest";
import { nextLength } from "./use-smooth-text";

describe("nextLength", () => {
  it("always moves at least one character while behind", () => {
    expect(nextLength(0, 10, 0)).toBe(1);
  });

  it("speeds up with the backlog so it never falls far behind", () => {
    // 1,000 characters behind, one 16 ms frame: reveal ~64, not 1.
    expect(nextLength(0, 1000, 16)).toBe(64);
    // A small backlog moves gently.
    expect(nextLength(95, 100, 16)).toBe(96);
  });

  it("never overshoots and is done when caught up", () => {
    expect(nextLength(99, 100, 1000)).toBe(100);
    expect(nextLength(100, 100, 16)).toBe(100);
  });
});
