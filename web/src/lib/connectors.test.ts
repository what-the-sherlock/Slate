import { describe, expect, it } from "vitest";
import { accountLine } from "@/app/(app)/integrations/manage-dialog";
import type { Source } from "@/lib/api/hooks";
import { isConnected, isEmail, itemNoun, SYNCABLE } from "./connectors";

describe("connector facts", () => {
  it("knows which sources sync and which are email", () => {
    expect(SYNCABLE).toContain("gmail");
    expect(SYNCABLE).not.toContain("upload");
    expect(SYNCABLE).not.toContain("gmail_mbox");
    expect(isEmail("gmail")).toBe(true);
    expect(isEmail("notion")).toBe(false);
    expect(isConnected("slack")).toBe(true);
    expect(isConnected("github")).toBe(false);
  });

  it("counts items in each connector's own words", () => {
    expect(itemNoun("gmail", 1284)).toBe("1,284 conversations");
    expect(itemNoun("linear", 1)).toBe("1 issue");
    expect(itemNoun("google_calendar", 3)).toBe("3 events");
    expect(itemNoun("mystery", 2)).toBe("2 items");
  });
});

describe("account line", () => {
  const source = { documents: 12, status: "synced", last_synced_at: new Date().toISOString() } as Source;

  it("says what to do when the account needs signing in", () => {
    expect(accountLine("gmail", source, true)).toMatch(/Reconnect to keep it up to date/);
  });

  it("explains a setup still in progress and a first sync", () => {
    expect(accountLine("gmail", undefined, false)).toMatch(/Setting up/);
    expect(accountLine("gmail", { ...source, status: "never" } as Source, false)).toBe("First sync queued");
  });

  it("shows the count and when it last synced", () => {
    expect(accountLine("notion", source, false)).toBe("12 pages · synced just now");
  });
});
