/** "14 min ago", "Yesterday, 16:20", "Oct 3" (UI_DESIGN §3.7). */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso);
  const seconds = Math.round((now.getTime() - then.getTime()) / 1000);
  if (seconds < 45) return "Just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24 && then.getDate() === now.getDate()) return `${hours} h ago`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const time = then.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (then.toDateString() === yesterday.toDateString()) return `Yesterday, ${time}`;
  return then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(then.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

export function absoluteTime(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", timeZoneName: "short" }) : "";
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}
