import { describe, expect, it } from "vitest";
import { syncMessage } from "./sync-result";

describe("syncMessage", () => {
  it("never calls a failure 'background'", () => {
    const m = syncMessage({ status: "failed", reason: "Slate hit an internal error while syncing." }, "Gmail");
    expect(m.tone).toBe("error");
    expect(m.text).toBe("Sync failed: Slate hit an internal error while syncing.");
    expect(m.text).not.toMatch(/background/);
  });

  it("falls back to a plain message when no reason came back", () => {
    expect(syncMessage({ status: "failed" }).text).toBe("Sync failed: something went wrong. Try again.");
  });

  it("passes a partial sync's own reason through", () => {
    const m = syncMessage({ status: "partial", reason: "This month's AI budget is spent." });
    expect(m).toEqual({ tone: "info", text: "This month's AI budget is spent." });
  });

  it("says done and background plainly", () => {
    expect(syncMessage({ status: "done" }, "octo/docs").text).toBe("Synced octo/docs.");
    expect(syncMessage({ status: "background" }).tone).toBe("info");
  });
});
