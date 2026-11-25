"use client";

import { useEffect, useRef } from "react";

/**
 * Cloudflare Turnstile, the bot check on sign-up, resend and password reset (ADR-0021).
 *
 * Wrapped once here so the rest of the app never touches `window.turnstile`. The widget
 * runs in "interaction-only" mode: most people never see it; a suspicious browser gets a
 * checkbox. A token is single use, so callers call `reset()` (via `onReady`) after each
 * submit. With no site key (local dev, where the API accepts any token) it reports a
 * placeholder token at once and renders nothing.
 */

const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
export const DEV_TOKEN = "dev-no-turnstile";

type TurnstileApi = {
  render: (el: HTMLElement, options: Record<string, unknown>) => string;
  reset: (id: string) => void;
  remove: (id: string) => void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let loading: Promise<TurnstileApi> | null = null;

function loadScript(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  loading ??= new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCRIPT_SRC;
    script.async = true;
    script.defer = true;
    script.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error("turnstile missing")));
    script.onerror = () => {
      loading = null; // allow a retry on the next mount
      reject(new Error("turnstile failed to load"));
    };
    document.head.appendChild(script);
  });
  return loading;
}

export function Turnstile({
  siteKey,
  onToken,
  onError,
  onReady,
  action,
}: {
  siteKey: string | null | undefined;
  onToken: (token: string | null) => void;
  onError?: (message: string) => void;
  /** Receives a reset function: call it after every submit (tokens are single use). */
  onReady?: (reset: () => void) => void;
  action: "signup" | "resend" | "reset";
}) {
  const box = useRef<HTMLDivElement>(null);
  // Callbacks change identity on every render; the widget must not re-mount for that. The
  // latest ones live in a ref, synced after each render (this effect is declared first, so it
  // runs before the widget effect). The reset function handed to the parent reads it too.
  const latest = useRef({ onToken, onError, onReady });
  useEffect(() => {
    latest.current = { onToken, onError, onReady };
  });
  const emitToken = (token: string | null) => latest.current.onToken(token);
  const emitError = (message: string) => latest.current.onError?.(message);
  const emitReady = (reset: () => void) => latest.current.onReady?.(reset);

  useEffect(() => {
    if (!siteKey) {
      emitToken(DEV_TOKEN);
      emitReady(() => emitToken(DEV_TOKEN));
      return;
    }
    let id: string | null = null;
    let cancelled = false;
    loadScript()
      .then((ts) => {
        if (cancelled || !box.current) return;
        id = ts.render(box.current, {
          sitekey: siteKey,
          action,
          appearance: "interaction-only",
          callback: (token: string) => emitToken(token),
          "expired-callback": () => emitToken(null),
          "error-callback": () => {
            emitToken(null);
            emitError("The bot check couldn't run. Refresh the page and try again.");
          },
        });
        emitReady(() => {
          emitToken(null);
          if (id) ts.reset(id);
        });
      })
      .catch(() => emitError("The bot check couldn't load. Check your connection and refresh."));
    return () => {
      cancelled = true;
      if (id && window.turnstile) window.turnstile.remove(id);
    };
  }, [siteKey, action]);

  return siteKey ? <div ref={box} className="flex min-h-0 justify-center empty:hidden" /> : null;
}
