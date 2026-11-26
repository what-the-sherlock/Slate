/**
 * Pure helpers for the Discord connect flow (ADR-0027). No React: unit-tested directly.
 */
import type { DiscordChannel } from "@/lib/api/hooks";

export const MAX_CHANNELS = 50;
const GRANT_KEY = "slate.discord.grant";
const GRANT_TTL_MS = 25 * 60_000; // the server's grant lasts 30 minutes
const SNOWFLAKE = /^[0-9]{15,21}$/;

export type Callback =
  | { kind: "install"; guildId: string; code: string; state: string }
  | { kind: "denied" } // the user pressed Cancel on Discord's page
  | { kind: "missing" };

/** Read Discord's redirect. Untrusted input: validate everything. */
export function parseCallback(params: URLSearchParams): Callback {
  if (params.get("error") === "access_denied") return { kind: "denied" };
  const guildId = params.get("guild_id") ?? "";
  const code = params.get("code") ?? "";
  const state = params.get("state") ?? "";
  if (!SNOWFLAKE.test(guildId) || !/^[A-Za-z0-9]{1,200}$/.test(code) || !state || state.length > 2000) {
    return { kind: "missing" };
  }
  return { kind: "install", guildId, code, state };
}

export type StoredGrant = { grant: string; at: number };

/** The grant survives a refresh in this tab only (sessionStorage), never in the URL. */
export function saveGrant(g: StoredGrant, storage: Storage | undefined = globalThis.sessionStorage): void {
  try {
    storage?.setItem(GRANT_KEY, JSON.stringify(g));
  } catch {
    // private mode or blocked storage: the flow still works without refresh support
  }
}

export function loadGrant(now = Date.now(), storage: Storage | undefined = globalThis.sessionStorage): StoredGrant | null {
  try {
    const raw = storage?.getItem(GRANT_KEY);
    if (!raw) return null;
    const g = JSON.parse(raw) as Partial<StoredGrant>;
    if (typeof g.grant !== "string" || typeof g.at !== "number") return null;
    return now - g.at < GRANT_TTL_MS ? (g as StoredGrant) : null;
  } catch {
    return null;
  }
}

export function clearGrant(storage: Storage | undefined = globalThis.sessionStorage): void {
  try {
    storage?.removeItem(GRANT_KEY);
  } catch {
    // ignore
  }
}

export type ChannelGroup = { category: string | null; channels: DiscordChannel[] };

/** Channels grouped by category, keeping the server's order (the API sends it sorted). */
export function groupChannels(channels: DiscordChannel[], query = ""): ChannelGroup[] {
  const q = query.trim().toLowerCase().replace(/^#/, "");
  const groups: ChannelGroup[] = [];
  for (const c of channels) {
    if (q && !c.name.toLowerCase().includes(q) && !(c.category ?? "").toLowerCase().includes(q)) continue;
    const last = groups[groups.length - 1];
    if (last && last.category === c.category) last.channels.push(c);
    else groups.push({ category: c.category, channels: [c] });
  }
  return groups;
}

/** Can still be ticked: selectable and not already connected. */
export function pickable(c: DiscordChannel): boolean {
  return c.selectable && !c.connected;
}

/** Toggle every pickable channel in view, respecting the per-server cap (connected count too). */
export function toggleAll(selected: Set<string>, visible: DiscordChannel[], connectedCount: number): Set<string> {
  const ids = visible.filter(pickable).map((c) => c.id);
  const allOn = ids.length > 0 && ids.every((id) => selected.has(id));
  const next = new Set(selected);
  if (allOn) {
    ids.forEach((id) => next.delete(id));
    return next;
  }
  for (const id of ids) {
    if (next.size + connectedCount >= MAX_CHANNELS) break;
    next.add(id);
  }
  return next;
}
