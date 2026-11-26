/**
 * Pure helpers for connecting a mailbox (ADR-0028). No React: unit-tested directly.
 */
import type { MailProvider } from "@/lib/api/hooks";

/** The provider an address most likely belongs to (the user can still change it). */
export function guessProvider(address: string, providers: MailProvider[]): MailProvider["id"] | null {
  const domain = address.trim().toLowerCase().split("@")[1] ?? "";
  if (!domain) return null;
  return providers.find((p) => p.domains.includes(domain))?.id ?? null;
}

/** Same shape check as the server (which is the authority). */
export function looksLikeAddress(address: string): boolean {
  return /^[^\s@]{1,64}@[^\s@]{1,253}\.[a-z]{2,63}$/i.test(address.trim());
}

/** App passwords are shown in groups ("abcd efgh ijkl mnop"); the spaces aren't part of it. */
export function normaliseAppPassword(raw: string): string {
  return raw.replace(/\s+/g, "");
}
