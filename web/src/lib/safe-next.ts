/**
 * Where to go after sign-in, from an untrusted `?next=` value. Only same-origin paths are
 * allowed: "//evil.com", "/\\evil.com", "https://evil.com" and "javascript:" all fall back
 * (security review: open redirect).
 */
export function safeNext(raw: string | null | undefined, origin: string, fallback = "/sources"): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return fallback;
  try {
    const url = new URL(raw, origin);
    if (url.origin !== origin) return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}
