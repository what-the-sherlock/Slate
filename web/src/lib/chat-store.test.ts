import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiProblem } from "@/lib/api/client";
import type { ChatEvent, ChatRequest, StoredMessage } from "@/lib/chat";
import { createChatStore, DEAD_MS, SLOW_MS } from "./chat-store";

const SID = "11111111-1111-1111-1111-111111111111";
const done = (extra: Partial<Extract<ChatEvent, { type: "done" }>["data"]> = {}): ChatEvent => ({
  type: "done",
  data: { v: 1, message_id: "a1", abstained: false, model: "m", latency_ms: 5, citations: [], closest: [], notices: [], mode: "grounded", ...extra },
});
const meta = (user = "u1"): ChatEvent => ({ type: "meta", data: { v: 1, session_id: SID, user_message_id: user } });
const token = (text: string): ChatEvent => ({ type: "token", data: { text } });

type Script = (body: ChatRequest, emit: (e: ChatEvent) => void, signal?: AbortSignal, onBytes?: () => void) => Promise<void>;

function setup(script: Script) {
  const calls: ChatRequest[] = [];
  let n = 0;
  const store = createChatStore({
    stream: (body, onEvent, signal, onBytes) => {
      calls.push(body);
      return script(body, onEvent, signal, onBytes);
    },
    newKey: () => `t${++n}`,
    tickMs: 1000,
  });
  return { store, calls };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("chat store", () => {
  it("streams a new chat, moves it to its session id and keeps the draft key working", async () => {
    const onSession = vi.fn();
    const { store, calls } = setup(async (_b, emit) => {
      emit(meta());
      emit(token("Hi "));
      emit(token("there."));
      emit(done({ mode: "chat" }));
    });
    await store.ask("draft:1", "hi", { onSession });
    await flush();
    expect(calls[0]!).toMatchObject({ question: "hi", session_id: null, replace_from: null });
    expect(onSession).toHaveBeenCalledWith(SID, "hi");
    const t = store.get("draft:1")!;
    expect(t).toBe(store.get(SID));
    expect(t.turns[0]!).toMatchObject({ stage: "done", text: "Hi there.", userMessageId: "u1" });
    expect(t.turns[0]!.done?.mode).toBe("chat");
    expect(store.isStreaming()).toBe(false);
  });

  it("regenerate replaces the turn and its successors and anchors on the stored question", async () => {
    let i = 0;
    const { store, calls } = setup(async (_b, emit) => {
      i++;
      emit(meta(`u${i}`));
      emit(token(`answer ${i}`));
      emit(done({ message_id: `a${i}` }));
    });
    await store.ask(SID, "one");
    await store.ask(SID, "two");
    const second = store.get(SID)!.turns[1]!;
    await store.regenerate(SID, second);
    expect(calls[2]!).toMatchObject({ question: "two", session_id: SID, replace_from: "u2" });
    expect(store.get(SID)!.turns.map((t) => [t.question, t.text])).toEqual([
      ["one", "answer 1"],
      ["two", "answer 3"],
    ]);
  });

  it("editing an earlier question drops every later turn", async () => {
    let i = 0;
    const { store, calls } = setup(async (_b, emit) => {
      i++;
      emit(meta(`u${i}`));
      emit(done());
    });
    await store.ask(SID, "one");
    await store.ask(SID, "two");
    await store.edit(SID, store.get(SID)!.turns[0]!, "one, edited");
    expect(calls[2]!.replace_from).toBe("u1");
    expect(store.get(SID)!.turns.map((t) => t.question)).toEqual(["one, edited"]);
  });

  it("a refusal before streaming gives the question back and restores replaced turns", async () => {
    let refuse = false;
    const { store } = setup(async (_b, emit) => {
      if (refuse) throw new ApiProblem({ type: "/problems/rate-limited", title: "Too many", status: 429, detail: "You're asking quickly.", retry_after_s: 60 });
      emit(meta());
      emit(done());
    });
    await store.ask(SID, "one");
    refuse = true;
    const onRefused = vi.fn();
    await store.edit(SID, store.get(SID)!.turns[0]!, "one again", { onRefused });
    expect(onRefused).toHaveBeenCalledWith("one again", expect.objectContaining({ kind: "rate-limited", retryAfterS: 60 }));
    expect(store.get(SID)!.turns.map((t) => t.question)).toEqual(["one"]); // nothing lost
  });

  it("stop keeps the partial answer and marks it stopped", async () => {
    const { store } = setup(
      (_b, emit, signal) =>
        new Promise((_, reject) => {
          emit(meta());
          emit(token("Partial"));
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const pending = store.ask(SID, "q");
    await flush();
    expect(store.streaming()).toEqual([SID]);
    store.stop(SID);
    await pending;
    expect(store.get(SID)!.turns[0]!).toMatchObject({ stage: "stopped", text: "Partial" });
  });

  it("a mid-stream error keeps the text and says so in plain words", async () => {
    const { store } = setup(async (_b, emit) => {
      emit(meta());
      emit(token("Half"));
      emit({ type: "error", data: { v: 1, type: "/problems/internal", title: "Internal error", status: 500, detail: "x", request_id: "req_1" } });
    });
    await store.ask(SID, "q");
    const t = store.get(SID)!.turns[0]!;
    expect(t.stage).toBe("error");
    expect(t.text).toBe("Half");
    expect(t.error).toMatchObject({ title: "The answer stopped before it finished", requestId: "req_1" });
  });

  it("a stream that closes without done becomes an error, not an endless spinner", async () => {
    const { store } = setup(async (_b, emit) => {
      emit(meta());
      emit(token("Cut"));
    });
    await store.ask(SID, "q");
    expect(store.get(SID)!.turns[0]!.stage).toBe("error");
  });

  it("a network failure is described without internals and the question stays", async () => {
    const { store } = setup(async () => {
      throw new TypeError("Failed to fetch");
    });
    await store.ask(SID, "q");
    const t = store.get(SID)!.turns[0]!;
    expect(t).toMatchObject({ stage: "error", question: "q" });
    expect(t.error?.detail).toMatch(/Your question is kept/);
  });

  it("only one answer at a time per chat", async () => {
    const { store, calls } = setup(() => new Promise(() => {}));
    void store.ask(SID, "a");
    void store.ask(SID, "b");
    await flush();
    expect(calls).toHaveLength(1);
  });

  it("hydrates a reopened chat once, and a trailing unanswered question is stopped", () => {
    const { store } = setup(async () => {});
    const msgs: StoredMessage[] = [
      { id: "u1", role: "user", content: "one", created_at: "", model: null, latency_ms: null, abstained: false, mode: "grounded", citations: [] },
      { id: "a1", role: "assistant", content: "General.", created_at: "", model: "m", latency_ms: 3, abstained: false, mode: "general", citations: [] },
      { id: "u2", role: "user", content: "two", created_at: "", model: null, latency_ms: null, abstained: false, mode: "grounded", citations: [] },
    ];
    store.hydrate(SID, msgs);
    const turns = store.get(SID)!.turns;
    expect(turns.map((t) => [t.stage, t.userMessageId])).toEqual([
      ["done", "u1"],
      ["stopped", "u2"],
    ]);
    expect(turns[0]!.done?.mode).toBe("general");
    store.hydrate(SID, []);
    expect(store.get(SID)!.turns).toHaveLength(2); // already held: not overwritten
    store.hydrate("22222222-2222-2222-2222-222222222222", []);
    expect(store.get("22222222-2222-2222-2222-222222222222")).toBeUndefined(); // stale link: nothing held
  });
});

describe("watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("flags a slow answer, then gives up when nothing arrives at all", async () => {
    let clock = 0;
    const store = createChatStore({
      now: () => clock,
      newKey: () => "t1",
      stream: (_b, emit, signal) =>
        new Promise((_, reject) => {
          emit(meta());
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    });
    const pending = store.ask(SID, "q");
    clock = SLOW_MS;
    await vi.advanceTimersByTimeAsync(1000);
    expect(store.get(SID)!.turns[0]!.slow).toBe(true);
    clock = DEAD_MS;
    await vi.advanceTimersByTimeAsync(1000);
    await pending;
    expect(store.get(SID)!.turns[0]!).toMatchObject({ stage: "error", error: { title: "This is taking too long" } });
  });

  it("keep-alive bytes keep a long answer alive", async () => {
    let clock = 0;
    let bytes: (() => void) | undefined;
    const store = createChatStore({
      now: () => clock,
      newKey: () => "t1",
      stream: (_b, emit, signal, onBytes) =>
        new Promise((_, reject) => {
          bytes = onBytes;
          emit(meta());
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    });
    void store.ask(SID, "q");
    for (let s = 15_000; s <= 120_000; s += 15_000) {
      clock = s;
      bytes?.(); // the server's ping
      await vi.advanceTimersByTimeAsync(1000);
    }
    expect(store.get(SID)!.turns[0]!.stage).not.toBe("error");
    store.stop(SID);
  });
});
