/**
 * Chat streaming client and answer state (UI_DESIGN §4.3, ARCHITECTURE §8 SSE contract).
 *
 * `fetch` + a ReadableStream parser, not EventSource: EventSource can only GET and can't
 * send our CSRF header. Pure pieces (parser, reducer, citation markdown) are unit-tested.
 * Integrity rule: a `[n]` becomes a citation badge only after the server's `citation`
 * event for that n; anything else stays plain text.
 */
import { readCsrfToken, toApiProblem } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type SourceOut = components["schemas"]["AnswerSourceOut"];
export type MetaEvent = components["schemas"]["MetaEvent"];
export type DoneEvent = components["schemas"]["DoneEvent"];
export type ErrorEvent = components["schemas"]["ErrorEvent"];
export type CitationEvent = components["schemas"]["CitationEvent"];

export type ChatEvent =
  | { type: "meta"; data: MetaEvent }
  | { type: "sources"; data: { sources: SourceOut[] } }
  | { type: "token"; data: { text: string } }
  | { type: "citation"; data: CitationEvent }
  | { type: "done"; data: DoneEvent }
  | { type: "error"; data: ErrorEvent };

const KNOWN = new Set(["meta", "sources", "token", "citation", "done", "error"]);

/** Incremental SSE parser (WHATWG event-stream format: `event:`/`data:` lines, blank line ends). */
export class SseParser {
  private buffer = "";
  private event = "message";
  private data: string[] = [];

  feed(chunk: string): ChatEvent[] {
    this.buffer += chunk;
    const out: ChatEvent[] = [];
    let nl: number;
    while ((nl = this.buffer.search(/\r?\n/)) !== -1) {
      const line = this.buffer.slice(0, nl);
      this.buffer = this.buffer.slice(nl + (this.buffer[nl] === "\r" ? 2 : 1));
      if (line === "") {
        if (this.data.length && KNOWN.has(this.event)) {
          try {
            out.push({ type: this.event, data: JSON.parse(this.data.join("\n")) } as ChatEvent);
          } catch {
            // A malformed event is dropped, never rendered.
          }
        }
        this.event = "message";
        this.data = [];
      } else if (line.startsWith(":")) {
        // comment / keep-alive ping
      } else if (line.startsWith("event:")) {
        this.event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        this.data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    return out;
  }
}

export type ChatRequest = components["schemas"]["ChatBody"];
export type AnswerMode = DoneEvent["mode"];

/**
 * POST /api/v1/chat and deliver events as they arrive. Errors before the stream starts
 * (validation, rate limit, budget, unverified) reject with an ApiProblem. `onBytes` fires
 * for every chunk read, keep-alive pings included, so a watchdog can tell a slow answer
 * from a dead connection.
 */
export async function streamChat(
  body: ChatRequest,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
  onBytes?: () => void,
): Promise<void> {
  const response = await globalThis.fetch(`${typeof window === "undefined" ? "" : window.location.origin}/api/v1/chat`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "content-type": "application/json",
      accept: "text/event-stream",
      "x-requested-with": "slate",
      "x-csrf-token": readCsrfToken(),
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    let problem: unknown = null;
    try {
      problem = await response.json();
    } catch {
      // not JSON
    }
    throw toApiProblem(problem, response.status);
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    onBytes?.();
    for (const event of parser.feed(value)) onEvent(event);
  }
  for (const event of parser.feed("\n\n")) onEvent(event);
}

// ---------------------------------------------------------------- answer state

export type Stage = "searching" | "reading" | "streaming" | "done" | "error" | "stopped";

/** What went wrong, in words for people (lib/chat-errors.ts). Never raw API text for 5xx. */
export interface TurnError {
  title: string;
  detail: string;
  requestId?: string | null;
  retryAfterS?: number;
}

export interface Turn {
  key: string;
  question: string;
  stage: Stage;
  sources: SourceOut[];
  text: string;
  cited: number[]; // server-validated citation numbers
  done?: DoneEvent;
  error?: TurnError;
  messageId?: string; // the assistant message (after done)
  userMessageId?: string; // the stored question (after meta): the anchor for regenerate/edit
  slow?: boolean; // no first token after 20 s
  startedAt?: number;
  replaces?: string; // the anchor this turn was asked to replace (kept for retries)
}

export function newTurn(question: string, key: string): Turn {
  return { key, question, stage: "searching", sources: [], text: "", cited: [] };
}

export function isBusy(turn: Turn): boolean {
  return turn.stage === "searching" || turn.stage === "reading" || turn.stage === "streaming";
}

export function reduceTurn(turn: Turn, event: ChatEvent): Turn {
  switch (event.type) {
    case "meta":
      return { ...turn, userMessageId: event.data.user_message_id };
    case "sources":
      return { ...turn, stage: "reading", sources: event.data.sources };
    case "token":
      return { ...turn, stage: "streaming", slow: false, text: turn.text + event.data.text };
    case "citation":
      return turn.cited.includes(event.data.n) ? turn : { ...turn, cited: [...turn.cited, event.data.n] };
    case "done":
      return {
        ...turn,
        stage: "done",
        done: event.data,
        messageId: event.data.message_id,
        cited: event.data.citations,
        sources: turn.sources.length ? turn.sources : event.data.closest,
      };
    case "error":
      return {
        ...turn,
        stage: "error",
        slow: false,
        // The server's in-band error is either an AppError written for people or a fixed
        // "stopped before it finished"; the request id is kept for support.
        error: {
          title: turn.text ? "The answer stopped before it finished" : "Slate couldn't answer this",
          detail: turn.text ? "What arrived is kept above. Try again for a full answer." : event.data.detail || event.data.title,
          requestId: event.data.request_id,
        },
      };
  }
}

/**
 * Turn validated `[n]` markers into links `cite:n` for the Markdown renderer, which shows
 * them as badges. Unvalidated markers are escaped so they render as literal text, except
 * that while an answer streams, a marker for a shown source becomes a muted placeholder
 * badge (`cite-pending:n`), so the text doesn't reflow when the server confirms it.
 */
export function citationMarkdown(text: string, valid: readonly number[], pending: readonly number[] = []): string {
  const ok = new Set(valid);
  const soon = new Set(pending);
  // The model can't mint its own cite: links; only ours (from bare [n]) are made below.
  text = text.replace(/\]\(\s*cite(-pending)?:/gi, "](#");
  return text.replace(/\[(\d{1,3})\]/g, (whole, n: string) =>
    ok.has(Number(n)) ? `[${n}](cite:${n})` : soon.has(Number(n)) ? `[${n}](cite-pending:${n})` : `\\[${n}\\]`,
  );
}

/** Markdown link policy: only http(s) links and our own cite: links survive (§3.11). */
export function safeUrl(url: string): string {
  if (/^cite(-pending)?:\d{1,3}$/.test(url)) return url;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? url : "";
  } catch {
    return "";
  }
}

/** Copy format: citations turned into footnotes, `[1] Title`. */
export function answerWithFootnotes(turn: Turn): string {
  const used = turn.sources.filter((s) => turn.cited.includes(s.n));
  if (!used.length) return turn.text;
  return `${turn.text}\n\n${used.map((s) => `[${s.n}] ${s.title}`).join("\n")}`;
}

export type StoredMessage = components["schemas"]["MessageOut"];

/**
 * Stored messages -> turns, for reopening a chat. Restored sources are placeholders (the
 * history endpoint returns citation ids, not titles). A question with no stored answer
 * (stopped, or the connection dropped) comes back as `stopped`, so it can be regenerated.
 */
export function turnsFromHistory(messages: readonly StoredMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      turns.push({ ...newTurn(m.content, m.id), stage: "stopped", userMessageId: m.id });
      continue;
    }
    const last = turns[turns.length - 1];
    if (!last || last.done) continue;
    const sources: SourceOut[] = m.citations
      .filter((c) => c.document_id)
      .map((c) => ({ n: c.n, chunk_id: c.chunk_id ?? 0, document_id: c.document_id!, ordinal: -1, title: `Source ${c.n}`, heading_path: null, snippet: "" }));
    Object.assign(last, {
      stage: "done",
      text: m.abstained ? "" : m.content,
      cited: m.citations.map((c) => c.n),
      sources,
      messageId: m.id,
      done: {
        v: 1,
        message_id: m.id,
        abstained: m.abstained,
        model: m.model,
        latency_ms: m.latency_ms ?? 0,
        citations: m.citations.map((c) => c.n),
        closest: [],
        notices: [],
        mode: m.mode ?? "grounded",
      },
    } satisfies Partial<Turn>);
  }
  return turns;
}
