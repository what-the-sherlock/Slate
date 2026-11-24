import { describe, expect, it } from "vitest";
import { safeNext } from "./safe-next";

const ORIGIN = "https://slate.example";

describe("safeNext", () => {
  it.each([
    ["/sources", "/sources"],
    ["/sources/abc?x=1#top", "/sources/abc?x=1#top"],
  ])("keeps same-origin path %s", (raw, expected) => {
    expect(safeNext(raw, ORIGIN)).toBe(expected);
  });

  it.each([
    "//evil.com",
    "/\\evil.com",
    "https://evil.com/x",
    "javascript:alert(1)",
    "evil.com",
    "",
    null,
  ])("rejects %s", (raw) => {
    expect(safeNext(raw, ORIGIN)).toBe("/home");
  });
});
