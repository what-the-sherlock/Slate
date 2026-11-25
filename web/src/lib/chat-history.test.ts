import { describe, expect, it } from "vitest";
import { chatTitle, groupByDay, matchesFilter } from "./chat-history";

const NOW = new Date(2026, 10, 26, 15, 0); // 26 Nov 2026, local time

function at(y: number, m: number, d: number, h = 9) {
  return { id: `${y}-${m}-${d}-${h}`, title: "t", created_at: new Date(y, m, d, h).toISOString() };
}

describe("groupByDay", () => {
  it("groups newest-first chats into relative day buckets", () => {
    const groups = groupByDay(
      [at(2026, 10, 26, 14), at(2026, 10, 26, 1), at(2026, 10, 25), at(2026, 10, 22), at(2026, 10, 5), at(2026, 8, 1), at(2025, 11, 30)],
      NOW,
    );
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ["Today", 2],
      ["Yesterday", 1],
      ["Previous 7 days", 1],
      ["Previous 30 days", 1],
      ["September", 1],
      ["2025", 1],
    ]);
  });

  it("is empty for no chats", () => {
    expect(groupByDay([], NOW)).toEqual([]);
  });
});

it("titles a new chat like the server does", () => {
  expect(chatTitle("  How   much\nleave? ")).toBe("How much leave?");
  expect(chatTitle("x".repeat(100))).toHaveLength(80);
});

it("filters by title, case-insensitively, untitled chats included", () => {
  expect(matchesFilter({ id: "1", title: "Leave policy", created_at: "" }, "LEAVE")).toBe(true);
  expect(matchesFilter({ id: "1", title: null, created_at: "" }, "untitled")).toBe(true);
  expect(matchesFilter({ id: "1", title: "Deploy", created_at: "" }, "leave")).toBe(false);
});
