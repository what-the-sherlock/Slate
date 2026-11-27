import { describe, expect, it } from "vitest";
import {
  accountSummary,
  brandKey,
  filterIntegrations,
  parseCallback,
  savePending,
  tabCounts,
  clearPending,
  peekPending,
  type Integration,
} from "./integrations";

function integration(over: Partial<Integration> = {}): Integration {
  return {
    id: "linear",
    name: "Linear",
    tagline: "Issues and projects",
    auth: "oauth2",
    mode: "indexed",
    state: "not_connected",
    connect_path: null,
    notice: null,
    connections: [],
    sources: 0,
    ...over,
  };
}

const CONNECTION = { id: "c1", account_label: "Kestrel", status: "connected" as const, last_error: null, connected_at: "2026-11-27T10:00:00Z" };

class MemoryStorage implements Storage {
  private data = new Map<string, string>();
  get length() {
    return this.data.size;
  }
  clear() {
    this.data.clear();
  }
  getItem(k: string) {
    return this.data.get(k) ?? null;
  }
  key(i: number) {
    return [...this.data.keys()][i] ?? null;
  }
  removeItem(k: string) {
    this.data.delete(k);
  }
  setItem(k: string, v: string) {
    this.data.set(k, v);
  }
}

describe("filtering", () => {
  const list = [
    integration(),
    integration({ id: "notion", name: "Notion", tagline: "Pages", state: "connected", connections: [CONNECTION] }),
    integration({ id: "gmail", name: "Gmail", tagline: "Your mail", state: "needs_reconnect", connections: [CONNECTION] }),
  ];

  it("splits by tab and counts", () => {
    expect(filterIntegrations(list, "connected", "").map((i) => i.id)).toEqual(["notion", "gmail"]);
    expect(filterIntegrations(list, "needs_sign_in", "").map((i) => i.id)).toEqual(["gmail"]);
    expect(tabCounts(list)).toEqual({ all: 3, connected: 2, needs_sign_in: 1 });
  });

  it("searches names and taglines, ignoring case and spaces", () => {
    expect(filterIntegrations(list, "all", "  MAIL ").map((i) => i.id)).toEqual(["gmail"]);
    expect(filterIntegrations(list, "all", "pages").map((i) => i.id)).toEqual(["notion"]);
  });
});

describe("account summary", () => {
  it("names the account, or counts older-flow sources", () => {
    expect(accountSummary(integration({ connections: [CONNECTION] }))).toBe("Kestrel");
    expect(accountSummary(integration({ connections: [CONNECTION, { ...CONNECTION, id: "c2" }] }))).toBe("Kestrel and 1 more");
    expect(accountSummary(integration({ sources: 1200 }))).toBe("1,200 sources");
    expect(accountSummary(integration())).toBeNull();
  });
});

describe("brand keys", () => {
  it("maps provider ids to marks", () => {
    expect(brandKey("google_drive")).toBe("googledrive");
    expect(brandKey("email")).toBe("imap");
    expect(brandKey("notion")).toBe("notion");
  });
});

describe("callback", () => {
  it("reads a code for the pending provider", () => {
    const params = new URLSearchParams("code=abc&state=s.t");
    expect(parseCallback(params, "linear")).toEqual({ kind: "code", provider: "linear", code: "abc", state: "s.t" });
  });

  it("treats a provider error as a cancel and a bad redirect as missing", () => {
    expect(parseCallback(new URLSearchParams("error=access_denied"), "linear")).toEqual({ kind: "denied", provider: "linear" });
    expect(parseCallback(new URLSearchParams("code=abc&state=s"), null)).toEqual({ kind: "missing" });
    expect(parseCallback(new URLSearchParams(`code=${"x".repeat(3000)}&state=s`), "linear")).toEqual({ kind: "missing" });
  });

  it("keeps a recent, valid pending provider until cleared", () => {
    const storage = new MemoryStorage();
    savePending("google_drive", storage);
    expect(peekPending(storage)).toBe("google_drive");
    expect(peekPending(storage)).toBe("google_drive"); // a peek doesn't consume it
    clearPending(storage);
    expect(peekPending(storage)).toBeNull();
    storage.setItem("slate.integrations.pending", JSON.stringify({ provider: "../x", at: Date.now() }));
    expect(peekPending(storage)).toBeNull();
    storage.setItem("slate.integrations.pending", JSON.stringify({ provider: "linear", at: Date.now() - 11 * 60_000 }));
    expect(peekPending(storage)).toBeNull();
  });
});
