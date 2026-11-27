/**
 * Facts about connectors the UI needs in several places (ADR-0028, ADR-0033). Pure.
 */

/** OAuth-connected sources (ADR-0033): synced by the shared engine, disconnected via their connection. */
export const CONNECTED = ["gmail", "notion", "linear", "google_calendar", "google_drive", "slack"] as const;

/** Sources that "Sync now" applies to. Uploads and .mbox imports don't sync. */
export const SYNCABLE: readonly string[] = ["github", "discord", "imap", ...CONNECTED];

/** Email is always private data and asks before it is shared (ADR-0026, ADR-0030). */
export function isEmail(connector: string): boolean {
  return connector === "imap" || connector === "gmail_mbox" || connector === "gmail";
}

export function isConnected(connector: string): boolean {
  return (CONNECTED as readonly string[]).includes(connector);
}

/** The words for what one indexed item is, per connector, for counts like "1,284 conversations". */
export function itemNoun(connector: string, n: number): string {
  const nouns: Record<string, [string, string]> = {
    gmail: ["conversation", "conversations"],
    imap: ["conversation", "conversations"],
    notion: ["page", "pages"],
    linear: ["issue", "issues"],
    google_calendar: ["event", "events"],
    google_drive: ["file", "files"],
    slack: ["channel day", "channel days"],
  };
  const [one, many] = nouns[connector] ?? ["item", "items"];
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
