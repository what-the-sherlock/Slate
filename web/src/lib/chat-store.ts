/**
 * The chat engine: every open chat's turns and the streams that fill them (UI_DESIGN §4.3).
 *
 * Why an external store (a plain module + `useSyncExternalStore`) and not component state:
 * a stream must outlive the component that started it. Switch to another chat mid-answer
 * and come back: the answer is still arriving (as in ChatGPT and Claude). Components only
 * *subscribe*; the store owns the AbortControllers, the watchdog and the error mapping.
 *
 * Threads are keyed by session id. A new chat starts under a `draft:` key; when the
 * server's `meta` event names the session, the thread moves to that id and the draft key
 * becomes an alias, so a view still holding the draft key keeps rendering it.
 *
 * Regenerate, edit and retry send `replace_from` (ADR-0024): the server supersedes that
 * question and everything after it, and the store drops those turns from the screen. If
 * the server refuses before anything was stored (a 4xx), the old turns come back and the
 * question is handed back to the composer: a failure never loses what the user wrote.
 */
import { useSyncExternalStore } from "react";
import { ApiProblem } from "@/lib/api/client";
import { isBusy, newTurn, reduceTurn, streamChat, turnsFromHistory, type ChatEvent, type ChatRequest, type StoredMessage, type Turn } from "@/lib/chat";
import { ChatTimeout, describeError, type DescribedError } from "@/lib/chat-errors";

export const SLOW_MS = 20_000; // no first token: "Still working…"
export const DEAD_MS = 75_000; // no bytes at all (the server pings every 15 s): give up

export interface Thread {
  key: string;
  sessionId: string | null;
  turns: Turn[];
}

export interface AskOptions {
  sourceIds?: string[];
  updatedAfter?: string;
  author?: string;
  /** Regenerate / edit / retry: this turn and every later one are replaced. */
  replace?: Turn;
  /** A new chat got its session id (update the URL, add it to the history list). */
  onSession?: (sessionId: string, question: string) => void;
  /** Refused before anything was stored: give the question back to the composer. */
  onRefused?: (question: string, error: DescribedError) => void;
  /** The stream ended, however it ended. */
  onSettled?: (sessionId: string | null) => void;
}

type StreamFn = (body: ChatRequest, onEvent: (e: ChatEvent) => void, signal?: AbortSignal, onBytes?: () => void) => Promise<void>;

interface Deps {
  stream?: StreamFn;
  now?: () => number;
  newKey?: () => string;
  tickMs?: number;
}

type Running = { controller: AbortController; reason?: "user" | "timeout" };

export function createChatStore({ stream = streamChat, now = Date.now, newKey = () => crypto.randomUUID(), tickMs = 1000 }: Deps = {}) {
  const threads = new Map<string, Thread>();
  const aliases = new Map<string, string>();
  const running = new Map<string, Running>(); // by turn key
  const listeners = new Set<() => void>();
  let streamingSnapshot: readonly string[] = [];
  let scheduled = false;

  function emit() {
    if (scheduled) return;
    scheduled = true;
    // Tokens parsed from one network chunk arrive together: notify once for all of them.
    queueMicrotask(() => {
      scheduled = false;
      const busy = [...threads.values()].filter((t) => t.turns.some(isBusy)).map((t) => t.key);
      if (busy.join() !== streamingSnapshot.join()) streamingSnapshot = busy;
      listeners.forEach((l) => l());
    });
  }

  const resolve = (key: string) => aliases.get(key) ?? key;

  function threadOf(turnKey: string): Thread | undefined {
    for (const t of threads.values()) if (t.turns.some((x) => x.key === turnKey)) return t;
    return undefined;
  }

  function patch(turnKey: string, fn: (t: Turn) => Turn) {
    const thread = threadOf(turnKey);
    if (!thread) return;
    threads.set(thread.key, { ...thread, turns: thread.turns.map((t) => (t.key === turnKey ? fn(t) : t)) });
    emit();
  }

  function findTurn(turnKey: string): Turn | undefined {
    return threadOf(turnKey)?.turns.find((t) => t.key === turnKey);
  }

  async function ask(threadKey: string, question: string, opts: AskOptions = {}): Promise<void> {
    const q = question.trim();
    if (!q) return;
    const k = resolve(threadKey);
    const thread = threads.get(k) ?? { key: k, sessionId: k.startsWith("draft:") ? null : k, turns: [] };
    if (thread.turns.some(isBusy)) return; // one answer at a time per chat

    const at = opts.replace ? thread.turns.findIndex((t) => t.key === opts.replace!.key) : -1;
    const kept = at === -1 ? thread.turns : thread.turns.slice(0, at);
    const replaced = at === -1 ? [] : thread.turns.slice(at);
    // The anchor is the stored question; a turn that failed before `meta` keeps the anchor
    // of the turn it was itself replacing, so retrying it still replaces the right thing.
    const replaceFrom = opts.replace ? (opts.replace.userMessageId ?? opts.replace.replaces) : undefined;
    const turn: Turn = { ...newTurn(q, newKey()), startedAt: now(), replaces: replaceFrom };
    threads.set(k, { ...thread, turns: [...kept, turn] });
    emit();

    const run: Running = { controller: new AbortController() };
    running.set(turn.key, run);
    let lastBytes = now();
    let gotMeta = false;
    let gotToken = false;
    const watchdog = setInterval(() => {
      const t = findTurn(turn.key);
      if (!t) return;
      if (!gotToken && !t.slow && now() - (t.startedAt ?? now()) >= SLOW_MS) patch(turn.key, (x) => ({ ...x, slow: true }));
      if (now() - lastBytes >= DEAD_MS) {
        run.reason = "timeout";
        run.controller.abort();
      }
    }, tickMs);

    try {
      await stream(
        { question: q, session_id: thread.sessionId, source_ids: opts.sourceIds ?? [], updated_after: opts.updatedAfter ?? null, author: opts.author || null, replace_from: replaceFrom ?? null },
        (event) => {
          if (event.type === "meta") {
            gotMeta = true;
            adopt(turn.key, event.data.session_id, q, opts);
          } else if (event.type === "token") {
            gotToken = true;
          }
          patch(turn.key, (t) => reduceTurn(t, event));
        },
        run.controller.signal,
        () => {
          lastBytes = now();
        },
      );
      const t = findTurn(turn.key);
      if (t && isBusy(t)) {
        // The stream closed without `done` or `error`: the connection was cut.
        patch(turn.key, (x) => ({
          ...x,
          stage: "error",
          slow: false,
          error: { title: "The answer stopped before it finished", detail: "The connection closed early. Try again." },
        }));
      }
    } catch (err) {
      if (run.reason === "user") {
        patch(turn.key, (t) => ({ ...t, stage: "stopped", slow: false }));
      } else if (run.reason === "timeout") {
        patch(turn.key, (t) => ({ ...t, stage: "error", slow: false, error: describeError(new ChatTimeout()) }));
      } else if (!gotMeta && err instanceof ApiProblem && err.isClientError) {
        // Refused before anything was stored: undo, restore what it would have replaced.
        const current = threadOf(turn.key);
        if (current) threads.set(current.key, { ...current, turns: [...current.turns.filter((t) => t.key !== turn.key), ...replaced] });
        emit();
        opts.onRefused?.(q, describeError(err));
      } else {
        const d = describeError(err);
        patch(turn.key, (t) => ({ ...t, stage: "error", slow: false, error: { title: d.title, detail: d.detail, requestId: d.requestId, retryAfterS: d.retryAfterS } }));
      }
    } finally {
      clearInterval(watchdog);
      running.delete(turn.key);
      emit();
      opts.onSettled?.(threadOf(turn.key)?.sessionId ?? null);
    }
  }

  function adopt(turnKey: string, sessionId: string, question: string, opts: AskOptions) {
    const thread = threadOf(turnKey);
    if (!thread || thread.sessionId) return;
    threads.delete(thread.key);
    threads.set(sessionId, { ...thread, key: sessionId, sessionId });
    aliases.set(thread.key, sessionId);
    emit();
    opts.onSession?.(sessionId, question);
  }

  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    get(key: string): Thread | undefined {
      return threads.get(resolve(key));
    },
    /** Session ids (or draft keys) of chats with an answer in progress. Stable between changes. */
    streaming(): readonly string[] {
      return streamingSnapshot;
    },
    isStreaming(): boolean {
      return running.size > 0;
    },
    ask,
    regenerate(threadKey: string, turn: Turn, opts: AskOptions = {}) {
      return ask(threadKey, turn.question, { ...opts, replace: turn });
    },
    edit(threadKey: string, turn: Turn, question: string, opts: AskOptions = {}) {
      return ask(threadKey, question, { ...opts, replace: turn });
    },
    stop(threadKey: string) {
      for (const t of threads.get(resolve(threadKey))?.turns ?? []) {
        const r = running.get(t.key);
        if (r) {
          r.reason = "user";
          r.controller.abort();
        }
      }
    },
    /** Forget every chat and stop every answer: the workspace changed, and nothing from the
     * old one may stay on screen or keep streaming (ADR-0030). */
    reset() {
      for (const r of running.values()) {
        r.reason = "user";
        r.controller.abort();
      }
      running.clear();
      threads.clear();
      aliases.clear();
      streamingSnapshot = [];
      emit();
    },
    /** Load a reopened chat from the server, unless this tab already holds it. */
    hydrate(sessionId: string, messages: readonly StoredMessage[]) {
      // No messages means a stale or someone else's link: hold nothing, so the view can
      // say "This chat isn't available" instead of showing an empty chat under that id.
      if (threads.get(sessionId)?.turns.length || !messages.length) return;
      threads.set(sessionId, { key: sessionId, sessionId, turns: turnsFromHistory(messages) });
      emit();
    },
  };
}

export type ChatStore = ReturnType<typeof createChatStore>;

/** The app's one store: lives as long as the tab, so streams survive navigation. */
export const chatStore = createChatStore();

export function useThread(key: string, store: ChatStore = chatStore): Thread | undefined {
  return useSyncExternalStore(store.subscribe, () => store.get(key), () => undefined);
}

const NONE: readonly string[] = [];
export function useStreamingChats(store: ChatStore = chatStore): readonly string[] {
  return useSyncExternalStore(store.subscribe, store.streaming, () => NONE);
}
