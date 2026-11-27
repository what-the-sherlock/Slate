/**
 * Pure helpers for the Integrations page and its OAuth callback (ADR-0032). No React:
 * unit-tested directly.
 */
import type { components } from "@/lib/api/schema";

export type Integration = components["schemas"]["IntegrationOut"];
export type IntegrationState = Integration["state"];
export type Tab = "all" | "connected" | "needs_sign_in";

const PENDING_KEY = "slate.integrations.pending";
const PENDING_TTL_MS = 10 * 60_000; // the server's state lasts 10 minutes
const PROVIDER_ID = /^[a-z][a-z0-9_]{1,31}$/;

/** Which brand mark each provider shows (simple-icons keys in components/brand). */
const BRAND: Record<string, string> = {
  email: "imap",
  google_calendar: "googlecalendar",
  google_drive: "googledrive",
};

export function brandKey(id: string): string {
  return BRAND[id] ?? id;
}

/** "Needs sign-in" is anything you've started that needs you: a dead grant. */
export function needsSignIn(i: Integration): boolean {
  return i.state === "needs_reconnect";
}

export function isConnected(i: Integration): boolean {
  return i.state === "connected" || i.state === "needs_reconnect";
}

export function filterIntegrations(list: readonly Integration[], tab: Tab, query: string): Integration[] {
  const q = query.trim().toLowerCase();
  return list.filter((i) => {
    if (tab === "connected" && !isConnected(i)) return false;
    if (tab === "needs_sign_in" && !needsSignIn(i)) return false;
    return !q || i.name.toLowerCase().includes(q) || i.tagline.toLowerCase().includes(q);
  });
}

export function tabCounts(list: readonly Integration[]): Record<Tab, number> {
  return {
    all: list.length,
    connected: list.filter(isConnected).length,
    needs_sign_in: list.filter(needsSignIn).length,
  };
}

/** The account line under a connected provider: who it's signed in as. */
export function accountSummary(i: Integration): string | null {
  if (i.connections.length > 0) {
    const labels = i.connections.map((c) => c.account_label ?? "Connected account");
    return labels.length === 1 ? labels[0]! : `${labels[0]} and ${labels.length - 1} more`;
  }
  if (i.sources > 0) return i.sources === 1 ? "1 source" : `${i.sources.toLocaleString()} sources`;
  return null;
}

// ---------------------------------------------------------------- callback

export type Callback =
  | { kind: "code"; provider: string; code: string; state: string }
  | { kind: "denied"; provider: string | null }
  | { kind: "missing" };

/** Remember which provider we sent the user to; the shared callback URL doesn't say. */
export function savePending(provider: string, storage: Storage | undefined = globalThis.sessionStorage): void {
  storage?.setItem(PENDING_KEY, JSON.stringify({ provider, at: Date.now() }));
}

/** The provider we sent this tab to, if it's recent (read without consuming it). */
export function peekPending(storage: Storage | undefined = globalThis.sessionStorage): string | null {
  const raw = storage?.getItem(PENDING_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { provider?: unknown; at?: unknown };
    const fresh = typeof parsed.at === "number" && Date.now() - parsed.at < PENDING_TTL_MS;
    return fresh && typeof parsed.provider === "string" && PROVIDER_ID.test(parsed.provider) ? parsed.provider : null;
  } catch {
    return null;
  }
}

export function clearPending(storage: Storage | undefined = globalThis.sessionStorage): void {
  storage?.removeItem(PENDING_KEY);
}

/** Read the provider's redirect. Untrusted input: validate everything. */
export function parseCallback(params: URLSearchParams, provider: string | null): Callback {
  if (params.get("error")) return { kind: "denied", provider };
  const code = params.get("code") ?? "";
  const state = params.get("state") ?? "";
  if (!provider || !code || code.length > 2048 || !state || state.length > 2048) return { kind: "missing" };
  return { kind: "code", provider, code, state };
}
