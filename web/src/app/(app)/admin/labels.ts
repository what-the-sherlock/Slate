import type { PillVariant } from "@/components/status-pill";
import type { AdminEventKind, AdminUser } from "@/lib/api/hooks";

/** Plain-language names for security events (UI_DESIGN §6: sentence case, no jargon). */
export const EVENT_LABEL: Record<AdminEventKind, string> = {
  signup: "Signed up",
  email_verified: "Verified email",
  login_success: "Signed in",
  login_failed: "Failed sign-in",
  login_throttled: "Sign-in blocked (too many tries)",
  logout: "Signed out",
  password_reset: "Reset password",
  account_disabled: "Account disabled",
  account_enabled: "Account re-enabled",
};

export const EVENT_TONE: Record<AdminEventKind, PillVariant> = {
  signup: "accent",
  email_verified: "success",
  login_success: "neutral",
  login_failed: "warning",
  login_throttled: "danger",
  logout: "outline",
  password_reset: "neutral",
  account_disabled: "danger",
  account_enabled: "success",
};

/** One status per account, most important first: disabled beats unverified. */
export function accountStatus(u: Pick<AdminUser, "disabled" | "email_verified">): { label: string; variant: PillVariant } {
  if (u.disabled) return { label: "Disabled", variant: "danger" };
  if (!u.email_verified) return { label: "Unverified", variant: "warning" };
  return { label: "Verified", variant: "success" };
}

/** "+3 vs last week", "−2 vs last week", "Same as last week". */
export function weekDelta(current: number, previous: number): string {
  const d = current - previous;
  if (d === 0) return "Same as last week";
  return `${d > 0 ? "+" : "−"}${Math.abs(d).toLocaleString("en-US")} vs last week`;
}

export function percent(part: number, whole: number): string {
  if (whole === 0) return "—";
  return `${Math.round((part / whole) * 100)}%`;
}

/** Axis/tooltip label for a bucket start ("2026-11-03"), read as a UTC calendar date. */
export function bucketLabel(start: string, bucket: "day" | "week" | "month", long = false): string {
  const d = new Date(`${start}T00:00:00Z`);
  if (bucket === "month") return d.toLocaleDateString(undefined, { month: long ? "long" : "short", year: "numeric", timeZone: "UTC" });
  const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  return bucket === "week" && long ? `Week of ${day}` : day;
}

/** A "nice" axis maximum (1, 2, 5 × 10ⁿ) at or above the data's max, never below 4. */
export function niceMax(max: number): number {
  if (max <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(max));
  for (const m of [1, 2, 5, 10]) if (m * p >= max) return m * p;
  return 10 * p;
}
