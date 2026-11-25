/**
 * Pure helpers for the Search page (docs/UI_DESIGN.md §4.4). No React, so they're unit-tested
 * directly. Highlighting is cosmetic only: what a user may see is decided by the API, in SQL.
 */

export const DATE_PRESETS = {
  any: { label: "Any time", days: null },
  week: { label: "Past week", days: 7 },
  month: { label: "Past month", days: 30 },
  year: { label: "Past year", days: 365 },
} as const;
export type DatePreset = keyof typeof DATE_PRESETS;

export interface SearchState {
  q: string;
  sources: string[];
  date: DatePreset;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Read the search state from the URL (`?q=&source=&date=`). Untrusted input: validate. */
export function parseSearchState(params: URLSearchParams): SearchState {
  const date = params.get("date") ?? "any";
  return {
    q: (params.get("q") ?? "").slice(0, 500),
    sources: params.getAll("source").filter((s) => UUID_RE.test(s)).slice(0, 20),
    date: date in DATE_PRESETS ? (date as DatePreset) : "any",
  };
}

export function toSearchParams(state: SearchState): URLSearchParams {
  const out = new URLSearchParams();
  if (state.q) out.set("q", state.q);
  for (const s of state.sources) out.append("source", s);
  if (state.date !== "any") out.set("date", state.date);
  return out;
}

/** The API's `updated_after` for a date preset, or undefined for "Any time". */
export function updatedAfter(preset: DatePreset, now: Date = new Date()): string | undefined {
  const days = DATE_PRESETS[preset].days;
  if (days === null) return undefined;
  return new Date(now.getTime() - days * 86_400_000).toISOString();
}

/** Words worth highlighting: no search operators, no 1-letter noise, longest first. */
export function queryTerms(query: string): string[] {
  const words = query
    .toLowerCase()
    .replace(/["()]/g, " ")
    .split(/\s+/)
    .map((w) => w.replace(/^-/, ""))
    .filter((w) => w.length >= 2 && w !== "or");
  return [...new Set(words)].sort((a, b) => b.length - a.length);
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export interface Segment {
  text: string;
  mark: boolean;
}

/** Split text into plain and highlighted segments. Rendered as text nodes, never as HTML. */
export function highlight(text: string, terms: string[]): Segment[] {
  if (!terms.length || !text) return [{ text, mark: false }];
  const re = new RegExp(`(${terms.map(escapeRe).join("|")})`, "gi");
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ text: text.slice(last, i), mark: false });
    out.push({ text: m[0], mark: true });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), mark: false });
  return out;
}

/** A window of ~`max` characters around the first matching term, with ellipses. */
export function snippet(content: string, terms: string[], max = 240): string {
  const flat = content.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  const lower = flat.toLowerCase();
  const hits = terms.map((t) => lower.indexOf(t)).filter((i) => i >= 0);
  const first = hits.length ? Math.min(...hits) : 0;
  const start = Math.max(0, Math.min(first - Math.floor(max / 3), flat.length - max));
  const body = flat.slice(start, start + max).trim();
  return `${start > 0 ? "…" : ""}${body}${start + max < flat.length ? "…" : ""}`;
}

// ---- Recent searches: a per-browser convenience only, so storage failures are ignored.
const RECENT_KEY = "slate.search.recent";
const RECENT_MAX = 5;

export function loadRecent(storage: Pick<Storage, "getItem"> | undefined = safeStorage()): string[] {
  try {
    const raw = JSON.parse(storage?.getItem(RECENT_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

export function pushRecent(
  query: string,
  storage: Pick<Storage, "getItem" | "setItem"> | undefined = safeStorage(),
): string[] {
  const q = query.trim();
  const next = q ? [q, ...loadRecent(storage).filter((r) => r !== q)].slice(0, RECENT_MAX) : loadRecent(storage);
  try {
    storage?.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* private mode or full storage: recents are optional */
  }
  notifyRecent();
  return next;
}

export function clearRecent(storage: Pick<Storage, "removeItem"> | undefined = safeStorage()): void {
  try {
    storage?.removeItem(RECENT_KEY);
  } catch {
    /* ignore */
  }
  notifyRecent();
}

// A tiny external store for useSyncExternalStore: the server snapshot is empty, so the
// server render and the first client render match (no hydration mismatch).
const listeners = new Set<() => void>();
const EMPTY: string[] = [];
let cache: { raw: string | null; value: string[] } = { raw: null, value: EMPTY };

function notifyRecent() {
  for (const l of listeners) l();
}

export const recentStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  getSnapshot(): string[] {
    let raw: string | null = null;
    try {
      raw = safeStorage()?.getItem(RECENT_KEY) ?? null;
    } catch {
      raw = null;
    }
    if (raw !== cache.raw) cache = { raw, value: loadRecent({ getItem: () => raw }) };
    return cache.value;
  },
  getServerSnapshot(): string[] {
    return EMPTY;
  },
};

function safeStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}
