/**
 * Chat failures in words for people (UI_DESIGN §4.3 error table). Every error says what
 * happened and what to do next; raw server text is shown only for 4xx problems, whose
 * details the API writes for users ("You're asking quickly…"). 5xx details and stack
 * traces never reach the screen: a reference id does, for support.
 */
import { ApiProblem } from "@/lib/api/client";
import type { TurnError } from "@/lib/chat";

export type ChatFailure =
  | "offline"
  | "timeout"
  | "rate-limited"
  | "budget"
  | "unavailable"
  | "refused" // any other 4xx before streaming (validation, not found, unverified)
  | "server";

export interface DescribedError extends TurnError {
  kind: ChatFailure;
}

/** Thrown by the chat store's watchdog when nothing arrives for too long. */
export class ChatTimeout extends Error {
  constructor() {
    super("timeout");
    this.name = "ChatTimeout";
  }
}

function online(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

export function describeError(err: unknown): DescribedError {
  if (err instanceof ChatTimeout) {
    return {
      kind: "timeout",
      title: "This is taking too long",
      detail: "Slate didn't hear back for over a minute, so it stopped waiting. Try again.",
    };
  }
  if (err instanceof ApiProblem) {
    const { status, detail, title, request_id, type, retry_after_s } = err.problem;
    if (status === 429 || type.endsWith("/rate-limited")) {
      return {
        kind: "rate-limited",
        title: "You're asking quickly",
        detail: detail ?? "Wait a moment, then try again.",
        retryAfterS: retry_after_s,
      };
    }
    if (type.endsWith("/budget-exceeded")) {
      return { kind: "budget", title: "AI allowance used up", detail: detail ?? "It resets soon. Try again later." };
    }
    if (status === 503 || type.endsWith("/upstream-unavailable")) {
      return {
        kind: "unavailable",
        title: "Slate's AI is unavailable right now",
        detail: "The model provider didn't respond. Try again in a moment.",
        requestId: request_id,
      };
    }
    if (status >= 400 && status < 500) {
      return { kind: "refused", title: "Slate couldn't send that", detail: detail ?? title };
    }
    return {
      kind: "server",
      title: "Something went wrong on our side",
      detail: "Try again. If it keeps happening, share the reference below.",
      requestId: request_id,
    };
  }
  // fetch() rejects with a TypeError when the network is down or the connection dropped.
  if (!online() || err instanceof TypeError) {
    return {
      kind: "offline",
      title: online() ? "Connection lost" : "You're offline",
      detail: "Check your connection, then try again. Your question is kept.",
    };
  }
  return {
    kind: "server",
    title: "Something went wrong",
    detail: "Try again. If it keeps happening, reload the page.",
  };
}

/** "Try again in 42 s" style countdown text; null once it's over. */
export function retryIn(retryAfterS: number | undefined, startedAt: number, now: number): string | null {
  if (!retryAfterS) return null;
  const left = Math.ceil(retryAfterS - (now - startedAt) / 1000);
  if (left <= 0) return null;
  if (left > 3600) return "Try again tomorrow";
  if (left > 90) return `Try again in ${Math.ceil(left / 60)} min`;
  return `Try again in ${left} s`;
}
