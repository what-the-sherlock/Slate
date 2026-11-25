"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Square } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ThinkingOrb } from "@/components/thinking-orb";
import { Button } from "@/components/ui/button";
import { ApiProblem, errorMessage } from "@/lib/api/client";
import { useChatMessages, useSources, type ChatMessageOut } from "@/lib/api/hooks";
import { newTurn, reduceTurn, streamChat, type SourceOut, type Turn } from "@/lib/chat";
import { formatCount } from "@/lib/format";
import { cn } from "@/lib/utils";
import { TurnView } from "./answer";
import { SourcesPanel } from "./sources-panel";

const LIMIT = 4000;
const COUNTER_AT = 3600;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Stored messages -> turns, for reopening a chat from the URL. */
export function turnsFromHistory(messages: ChatMessageOut[]): Turn[] {
  const turns: Turn[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      turns.push({ ...newTurn(m.content, m.id), stage: "done" });
      continue;
    }
    const last = turns[turns.length - 1];
    if (!last) continue;
    const sources: SourceOut[] = m.citations
      .filter((c) => c.document_id)
      .map((c) => ({ n: c.n, chunk_id: c.chunk_id ?? 0, document_id: c.document_id!, ordinal: -1, title: `Source ${c.n}`, heading_path: null, snippet: "" }));
    Object.assign(last, {
      text: m.abstained ? "" : m.content,
      cited: m.citations.map((c) => c.n),
      sources,
      messageId: m.id,
      done: { v: 1, message_id: m.id, abstained: m.abstained, model: m.model, latency_ms: m.latency_ms ?? 0, citations: m.citations.map((c) => c.n), closest: [], notices: [] },
    });
  }
  return turns;
}

/** Chat (UI_DESIGN §4.3): empty state, conversation, composer, sources panel. */
export function ChatView() {
  const router = useRouter();
  const params = useSearchParams();
  const client = useQueryClient();
  const rawSession = params.get("c");
  const urlSession = rawSession && UUID_RE.test(rawSession) ? rawSession : null;
  const history = useChatMessages(urlSession);
  const sources = useSources();
  const [session, setSession] = useState<string | null>(urlSession);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [composerError, setComposerError] = useState<string | null>(null);
  const [panel, setPanel] = useState<{ key: string; n: number | null } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const atBottom = useRef(true);

  // Reopening a chat: load its stored turns once.
  useEffect(() => {
    if (history.data && urlSession && session === urlSession && turns.length === 0) setTurns(turnsFromHistory(history.data));
  }, [history.data, urlSession, session, turns.length]);

  // "New chat" (the URL lost ?c=) resets the screen.
  useEffect(() => {
    if (!urlSession && session && !abort.current) {
      setSession(null);
      setTurns([]);
      setPanel(null);
    }
  }, [urlSession, session]);

  useEffect(() => {
    const onScroll = () => {
      atBottom.current = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (atBottom.current) bottom.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && /^(INPUT|TEXTAREA)$/.test(e.target.tagName);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const streaming = turns.some((t) => t.stage === "searching" || t.stage === "reading" || t.stage === "streaming");
  const counts = useMemo(() => {
    const list = sources.data ?? [];
    return { sources: list.length, documents: list.reduce((n, s) => n + s.documents, 0) };
  }, [sources.data]);

  const update = (key: string, fn: (t: Turn) => Turn) => setTurns((all) => all.map((t) => (t.key === key ? fn(t) : t)));

  async function ask(question: string) {
    const q = question.trim();
    if (!q || q.length > LIMIT || streaming) return;
    setComposerError(null);
    const key = crypto.randomUUID();
    setTurns((all) => [...all, newTurn(q, key)]);
    atBottom.current = true;
    const controller = new AbortController();
    abort.current = controller;
    try {
      await streamChat(
        { question: q, session_id: session },
        (event) => {
          if (event.type === "meta" && event.data.session_id !== session) {
            setSession(event.data.session_id);
            router.replace(`/chat?c=${event.data.session_id}`, { scroll: false });
          }
          update(key, (t) => reduceTurn(t, event));
        },
        controller.signal,
      );
      void client.invalidateQueries({ queryKey: ["chat-sessions"] });
    } catch (err) {
      if (controller.signal.aborted) {
        update(key, (t) => ({ ...t, stage: "stopped" }));
      } else if (err instanceof ApiProblem && err.isClientError) {
        // Refused before streaming (limit, budget, validation): take the question back.
        setTurns((all) => all.filter((t) => t.key !== key));
        setDraft(q);
        setComposerError(errorMessage(err));
      } else {
        update(key, (t) => ({ ...t, stage: "error", error: { message: errorMessage(err), requestId: err instanceof ApiProblem ? err.problem.request_id : null } }));
      }
    } finally {
      abort.current = null;
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const q = draft;
    if (!q.trim() || q.length > LIMIT) return;
    setDraft("");
    void ask(q);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    } else if (e.key === "ArrowUp" && !draft) {
      const last = turns[turns.length - 1];
      if (last) {
        e.preventDefault();
        setDraft(last.question);
      }
    }
  }

  const panelTurn = panel ? turns.find((t) => t.key === panel.key) : undefined;
  const empty = turns.length === 0 && !(urlSession && history.isPending);

  return (
    <div className="flex min-h-svh">
      <div className="mx-auto flex w-full max-w-[760px] min-w-0 flex-1 flex-col px-4 sm:px-6">
        {empty ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-4 py-16 text-center">
            <ThinkingOrb state="breathing" size={64} label="Slate is ready" />
            <h1 className="text-[28px] font-semibold text-fg">What do you want to know?</h1>
            <p className="text-body text-fg-subtle">
              {counts.sources
                ? `Searching ${formatCount(counts.sources)} sources · ${formatCount(counts.documents)} documents you can access`
                : "Your workspace is empty."}{" "}
              {!counts.documents ? (
                <Link href="/sources?add=1" className="font-medium text-brand hover:underline">
                  Upload documents
                </Link>
              ) : null}
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-10 py-8">
            {turns.map((t) => (
              <TurnView key={t.key} turn={t} onOpenSource={(turn, n) => setPanel({ key: turn.key, n })} onRetry={(turn) => void ask(turn.question)} />
            ))}
            <div ref={bottom} />
          </div>
        )}

        <form onSubmit={onSubmit} className="sticky bottom-0 bg-gradient-to-t from-bg via-bg to-transparent pt-6 pb-[max(16px,env(safe-area-inset-bottom))]">
          {composerError ? (
            <p role="alert" className="mb-2 text-caption text-danger">
              {composerError}
            </p>
          ) : null}
          <div className="rounded-lg border border-border-strong bg-bg p-3 shadow-sm focus-within:border-brand">
            <label htmlFor="composer" className="sr-only">
              Ask a question
            </label>
            <textarea
              ref={input}
              id="composer"
              rows={1}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setComposerError(null);
                e.target.style.height = "auto";
                e.target.style.height = `${Math.min(e.target.scrollHeight, 8 * 24)}px`;
              }}
              onKeyDown={onKeyDown}
              placeholder="Ask anything about your sources…"
              className="block max-h-48 w-full resize-none bg-transparent text-[15px] leading-6 text-fg outline-none placeholder:text-fg-subtle"
            />
            <div className="mt-2 flex items-center gap-3">
              {draft.length >= COUNTER_AT ? (
                <span className={cn("text-data tabular-nums", draft.length > LIMIT ? "text-danger" : "text-fg-subtle")}>
                  {draft.length.toLocaleString()} / {LIMIT.toLocaleString()}
                </span>
              ) : null}
              <span className="ml-auto hidden text-data text-fg-subtle sm:inline">↵ to send, ⇧↵ for a new line</span>
              {streaming ? (
                <Button type="button" variant="secondary" size="icon" aria-label="Stop" onClick={() => abort.current?.abort()}>
                  <Square />
                </Button>
              ) : (
                <Button type="submit" variant="primary" size="icon" aria-label="Send" disabled={!draft.trim() || draft.length > LIMIT}>
                  <ArrowUp />
                </Button>
              )}
            </div>
          </div>
        </form>
      </div>

      {panel && panelTurn ? (
        <SourcesPanel
          sources={panelTurn.sources}
          cited={panelTurn.cited}
          selected={panel.n}
          onSelect={(n) => setPanel({ key: panel.key, n })}
          onClose={() => setPanel(null)}
        />
      ) : null}
    </div>
  );
}
