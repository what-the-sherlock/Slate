/**
 * The chat history list (UI_DESIGN §4.3): chats grouped by when they started, in the
 * viewer's local time. Pure, so the grouping is unit-tested with a fixed "now".
 */

export interface HistoryItem {
  id: string;
  title: string | null;
  created_at: string;
}

export interface HistoryGroup<T extends HistoryItem> {
  label: string;
  items: T[];
}

const DAY = 86_400_000;

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function label(created: Date, now: Date): string {
  const days = Math.round((startOfDay(now) - startOfDay(created)) / DAY);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return created.getFullYear() === now.getFullYear()
    ? created.toLocaleString("en", { month: "long" })
    : String(created.getFullYear());
}

/** Newest first in, newest first out; groups keep their first-seen order. */
export function groupByDay<T extends HistoryItem>(items: readonly T[], now: Date = new Date()): HistoryGroup<T>[] {
  const groups: HistoryGroup<T>[] = [];
  for (const item of items) {
    const name = label(new Date(item.created_at), now);
    const last = groups[groups.length - 1];
    if (last && last.label === name) last.items.push(item);
    else groups.push({ label: name, items: [item] });
  }
  return groups;
}

/** The title a new chat gets on the server (core/services/answer.py `_title`), for the optimistic row. */
export function chatTitle(question: string): string {
  const oneLine = question.split(/\s+/).filter(Boolean).join(" ");
  return oneLine.length <= 80 ? oneLine : `${oneLine.slice(0, 79).trimEnd()}…`;
}

export function matchesFilter(item: HistoryItem, filter: string): boolean {
  const q = filter.trim().toLowerCase();
  return !q || (item.title ?? "Untitled chat").toLowerCase().includes(q);
}
