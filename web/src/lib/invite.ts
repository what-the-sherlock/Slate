/**
 * The invitation token from the link's fragment (#token), kept in sessionStorage so a
 * sign-in or sign-up detour in the same tab doesn't lose it. Never put in a URL query.
 */
const KEY = "slate.invite.token";
const TOKEN = /^[A-Za-z0-9_-]{20,100}$/;

export function readInviteToken(hash: string, storage: Storage | undefined = globalThis.sessionStorage): string | null {
  const fromHash = hash.replace(/^#/, "");
  if (TOKEN.test(fromHash)) {
    try {
      storage?.setItem(KEY, fromHash);
    } catch {
      // blocked storage: the token still works for this page view
    }
    return fromHash;
  }
  try {
    const stored = storage?.getItem(KEY) ?? null;
    return stored && TOKEN.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

export function clearInviteToken(storage: Storage | undefined = globalThis.sessionStorage): void {
  try {
    storage?.removeItem(KEY);
  } catch {
    // ignore
  }
}
