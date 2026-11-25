/**
 * Pure helpers for the GitHub connect flow (ADR-0023). No React: unit-tested directly.
 */
import type { GitHubRepo } from "@/lib/api/hooks";

export const MAX_REPOS = 20;
const GRANT_KEY = "slate.github.grant";
const GRANT_TTL_MS = 25 * 60_000; // the server's grant lasts 30 minutes

export type Callback =
  | { kind: "install"; installationId: number; code: string; state: string }
  | { kind: "approval" } // an organisation owner must approve the install first
  | { kind: "missing" };

/** Read GitHub's redirect. Untrusted input: validate everything. */
export function parseCallback(params: URLSearchParams): Callback {
  if (params.get("setup_action") === "request") return { kind: "approval" };
  const id = Number(params.get("installation_id"));
  const code = params.get("code") ?? "";
  const state = params.get("state") ?? "";
  if (!Number.isSafeInteger(id) || id <= 0 || !code || !state || code.length > 200 || state.length > 2000) {
    return { kind: "missing" };
  }
  return { kind: "install", installationId: id, code, state };
}

export type StoredGrant = { grant: string; installationId: number; at: number };

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
    if (typeof g.grant !== "string" || typeof g.installationId !== "number" || typeof g.at !== "number") return null;
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

/** Case-insensitive match on name and description. */
export function filterRepos(repos: GitHubRepo[], query: string): GitHubRepo[] {
  const q = query.trim().toLowerCase();
  if (!q) return repos;
  return repos.filter((r) => r.full_name.toLowerCase().includes(q) || (r.description ?? "").toLowerCase().includes(q));
}

/** Toggle every selectable repo in view, respecting the per-connect cap. */
export function toggleAll(selected: Set<string>, visible: GitHubRepo[]): Set<string> {
  const selectable = visible.filter((r) => !r.connected).map((r) => r.full_name);
  const allOn = selectable.length > 0 && selectable.every((n) => selected.has(n));
  const next = new Set(selected);
  if (allOn) {
    selectable.forEach((n) => next.delete(n));
    return next;
  }
  for (const n of selectable) {
    if (next.size >= MAX_REPOS) break;
    next.add(n);
  }
  return next;
}

export function manageInstallationUrl(installationId: number): string {
  return `https://github.com/settings/installations/${installationId}`;
}
