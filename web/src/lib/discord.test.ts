import { describe, expect, it } from "vitest";
import type { DiscordChannel } from "@/lib/api/hooks";
import { clearGrant, groupChannels, loadGrant, MAX_CHANNELS, parseCallback, saveGrant, toggleAll } from "./discord";

const G = "200000000000000000";

function ch(id: number, over: Partial<DiscordChannel> = {}): DiscordChannel {
  return {
    id: `3${String(id).padStart(17, "0")}`, // string ids: snowflakes exceed Number precision
    name: `c${id}`,
    kind: "text",
    category: null,
    private: false,
    selectable: true,
    reason: null,
    connected: false,
    ...over,
  };
}

describe("parseCallback", () => {
  it("accepts a well-formed redirect", () => {
    expect(parseCallback(new URLSearchParams(`code=abc123&state=st&guild_id=${G}`))).toEqual({
      kind: "install",
      guildId: G,
      code: "abc123",
      state: "st",
    });
  });
  it("recognises a cancel", () => {
    expect(parseCallback(new URLSearchParams("error=access_denied")).kind).toBe("denied");
  });
  it.each([
    "code=abc&state=st",
    `code=abc&guild_id=${G}`,
    `code=a%20b&state=st&guild_id=${G}`,
    "code=abc&state=st&guild_id=../x",
    `code=abc&state=${"x".repeat(2001)}&guild_id=${G}`,
  ])("rejects %s", (q) => {
    expect(parseCallback(new URLSearchParams(q)).kind).toBe("missing");
  });
});

describe("grant storage", () => {
  it("round-trips and expires", () => {
    saveGrant({ grant: "g", at: 1000 });
    expect(loadGrant(2000)?.grant).toBe("g");
    expect(loadGrant(1000 + 26 * 60_000)).toBeNull();
    clearGrant();
    expect(loadGrant(2000)).toBeNull();
  });
  it("ignores garbage", () => {
    sessionStorage.setItem("slate.discord.grant", "{nope");
    expect(loadGrant()).toBeNull();
  });
});

describe("groupChannels", () => {
  it("keeps server order and groups consecutive categories", () => {
    const list = [ch(1), ch(2, { category: "Team" }), ch(3, { category: "Team" }), ch(4, { category: "Ops" })];
    expect(groupChannels(list).map((g) => [g.category, g.channels.length])).toEqual([
      [null, 1],
      ["Team", 2],
      ["Ops", 1],
    ]);
  });
  it("filters by name or category, ignoring a leading #", () => {
    const list = [ch(1, { name: "general" }), ch(2, { name: "random", category: "Fun" })];
    expect(groupChannels(list, "#gen").flatMap((g) => g.channels.map((c) => c.name))).toEqual(["general"]);
    expect(groupChannels(list, "fun").flatMap((g) => g.channels.map((c) => c.name))).toEqual(["random"]);
  });
});

describe("toggleAll", () => {
  it("selects only pickable channels and clears on the second press", () => {
    const list = [ch(1), ch(2, { selectable: false }), ch(3, { connected: true })];
    const on = toggleAll(new Set(), list, 1);
    expect([...on]).toEqual([list[0]!.id]);
    expect(toggleAll(on, list, 1).size).toBe(0);
  });
  it("respects the per-server cap including connected channels", () => {
    const list = Array.from({ length: 60 }, (_, i) => ch(i));
    expect(toggleAll(new Set(), list, 45).size).toBe(MAX_CHANNELS - 45);
  });
});
