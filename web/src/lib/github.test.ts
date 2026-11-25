import { describe, expect, it } from "vitest";
import type { GitHubRepo } from "@/lib/api/hooks";
import { clearGrant, filterRepos, loadGrant, MAX_REPOS, parseCallback, saveGrant, toggleAll } from "./github";

const repo = (full_name: string, extra: Partial<GitHubRepo> = {}): GitHubRepo => ({
  id: full_name.length,
  full_name,
  private: true,
  description: null,
  html_url: `https://github.com/${full_name}`,
  connected: false,
  ...extra,
});

class MemoryStorage implements Storage {
  private m = new Map<string, string>();
  get length() {
    return this.m.size;
  }
  clear() {
    this.m.clear();
  }
  getItem(k: string) {
    return this.m.get(k) ?? null;
  }
  key(i: number) {
    return [...this.m.keys()][i] ?? null;
  }
  removeItem(k: string) {
    this.m.delete(k);
  }
  setItem(k: string, v: string) {
    this.m.set(k, v);
  }
}

describe("parseCallback", () => {
  it("reads a complete install redirect", () => {
    const p = new URLSearchParams("installation_id=42&code=abc&state=s&setup_action=install");
    expect(parseCallback(p)).toEqual({ kind: "install", installationId: 42, code: "abc", state: "s" });
  });
  it("recognises an install waiting for an org owner's approval", () => {
    expect(parseCallback(new URLSearchParams("setup_action=request"))).toEqual({ kind: "approval" });
  });
  it.each(["", "installation_id=x&code=a&state=s", "installation_id=-1&code=a&state=s", "installation_id=4&state=s", "installation_id=4&code=a"])(
    "treats %j as missing",
    (q) => {
      expect(parseCallback(new URLSearchParams(q)).kind).toBe("missing");
    },
  );
});

describe("grant storage", () => {
  it("round-trips, expires and clears", () => {
    const s = new MemoryStorage();
    saveGrant({ grant: "g", installationId: 4, at: 1_000 }, s);
    expect(loadGrant(2_000, s)?.grant).toBe("g");
    expect(loadGrant(1_000 + 26 * 60_000, s)).toBeNull(); // older than the server's grant
    clearGrant(s);
    expect(loadGrant(2_000, s)).toBeNull();
  });
  it("ignores garbage", () => {
    const s = new MemoryStorage();
    s.setItem("slate.github.grant", "{not json");
    expect(loadGrant(0, s)).toBeNull();
  });
});

describe("repo picking", () => {
  it("filters by name and description", () => {
    const repos = [repo("octo/docs", { description: "Handbook" }), repo("octo/api")];
    expect(filterRepos(repos, "HAND").map((r) => r.full_name)).toEqual(["octo/docs"]);
    expect(filterRepos(repos, "  ")).toHaveLength(2);
  });
  it("select all skips connected repos, respects the cap, and toggles off", () => {
    const repos = [repo("a/connected", { connected: true }), ...Array.from({ length: 25 }, (_, i) => repo(`a/r${i}`))];
    const on = toggleAll(new Set(), repos);
    expect(on.size).toBe(MAX_REPOS);
    expect(on.has("a/connected")).toBe(false);
    const few = repos.slice(0, 3);
    expect(toggleAll(toggleAll(new Set(), few), few).size).toBe(0);
  });
});
