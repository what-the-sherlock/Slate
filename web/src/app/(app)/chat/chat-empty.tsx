"use client";

import { ArrowRight, CircleAlert, Loader, MessageCircle, Sparkles } from "lucide-react";
import Link from "next/link";
import { ConnectorIcon } from "@/components/brand";
import { ThinkingOrb } from "@/components/thinking-orb";
import type { Source } from "@/lib/api/hooks";
import { formatCount } from "@/lib/format";
import { delay, REVEAL } from "@/lib/reveal";

interface Suggestion {
  text: string;
  connector: string | null;
}

/** Starter questions from the user's own sources, plus one general starter (ADR-0025). */
export function suggestionsFor(sources: Source[]): Suggestion[] {
  const out: Suggestion[] = [];
  const ready = sources.filter((s) => s.documents > 0);
  for (const s of ready.filter((x) => x.connector === "github").slice(0, 2)) {
    out.push({ text: `What are the open issues and recent pull requests in ${s.name}?`, connector: "github" });
    out.push({ text: `Summarise what ${s.name} does, from its README and docs.`, connector: "github" });
  }
  if (ready.some((s) => s.connector === "upload")) {
    out.push({ text: "Summarise the key points of my uploaded documents.", connector: "upload" });
  }
  out.push({ text: "What can you help me with?", connector: null });
  return out.slice(0, 4);
}

/**
 * New chat (UI_DESIGN §4.3): the breathing orb, the question, a status line that explains
 * what Slate can search *right now* (including a repo still syncing or failing to sync,
 * instead of a bare "0 documents"), and starter questions. A starter fills the composer;
 * it doesn't send, so the user stays in control.
 */
export function ChatEmpty({ sources, loading, onPick }: { sources: Source[]; loading: boolean; onPick: (text: string) => void }) {
  const documents = sources.reduce((n, s) => n + s.documents, 0);
  const syncing = sources.filter((s) => (s.busy || s.status === "never") && s.connector !== "upload");
  // Anything that needs attention says why (P0-2): failed, waiting on the worker, empty.
  const failing = sources.filter((s) => !s.healthy && !s.busy && (s.status !== "partial" || s.last_error));
  const suggestions = suggestionsFor(sources);

  return (
    <div className="mx-auto flex w-full max-w-[640px] flex-1 flex-col items-center justify-center gap-6 px-4 py-10 text-center">
      <div className={REVEAL}>
        <ThinkingOrb state="breathing" size={64} label="Slate is ready" />
      </div>
      <div className="flex flex-col gap-2">
        <h1 className={`${REVEAL} text-[28px] leading-tight font-semibold tracking-[-0.02em] text-fg`} style={delay(60)}>
          What do you want to know?
        </h1>
        <div className={`${REVEAL} flex flex-col items-center gap-1.5 text-body text-fg-subtle`} style={delay(120)}>
          {loading ? (
            <span className="h-5" />
          ) : !sources.length ? (
            <p>
              Ask a general question, or{" "}
              <Link href="/sources?add=1" className="font-medium text-brand hover:underline">
                add a source
              </Link>{" "}
              for answers cited from your own documents.
            </p>
          ) : (
            <p>
              Searching {formatCount(sources.length)} source{sources.length === 1 ? "" : "s"} · {formatCount(documents)} document
              {documents === 1 ? "" : "s"} you can access
            </p>
          )}
          {syncing.map((s) => (
            <p key={s.id} className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-subtle px-3 py-1 text-caption wrap-anywhere text-fg-muted">
              <Loader className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              <span>
                <strong className="font-medium text-fg">{s.name}</strong>{" "}
                {s.status === "never" ? "hasn't synced yet." : "is still syncing. Answers will include it once it's done."}{" "}
                <Link href="/sources" className="font-medium text-brand hover:underline">
                  View
                </Link>
              </span>
            </p>
          ))}
          {failing.map((s) => (
            <div
              key={s.id}
              role="status"
              className="flex w-full max-w-[520px] min-w-0 items-start gap-2.5 rounded-lg border border-danger-border bg-danger-subtle px-3 py-2.5 text-left text-caption"
            >
              <CircleAlert className="mt-px size-4 shrink-0 text-danger" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="font-medium wrap-anywhere text-fg">
                  {s.name} {s.status === "empty" ? "has nothing indexed" : "couldn't sync"}
                </p>
                {s.status_reason ?? s.last_error ? (
                  <p className="mt-0.5 line-clamp-2 wrap-anywhere text-fg-muted" title={s.status_reason ?? s.last_error ?? undefined}>
                    {s.status_reason ?? s.last_error}
                  </p>
                ) : null}
              </div>
              <Link href="/sources" className="shrink-0 self-center rounded-md px-2 py-1 font-medium whitespace-nowrap text-brand hover:bg-bg/60">
                Fix
              </Link>
            </div>
          ))}
        </div>
      </div>

      {!loading ? (
        <ul className="grid w-full gap-2 sm:grid-cols-2" aria-label="Suggested questions">
          {suggestions.map((s, i) => (
            <li key={s.text} className={REVEAL} style={delay(200 + i * 50)}>
              <button
                type="button"
                onClick={() => onPick(s.text)}
                className="group flex h-full w-full items-start gap-3 rounded-xl border border-border bg-bg px-3.5 py-3 text-left text-body text-fg-body shadow-xs transition-[border-color,background-color,transform,box-shadow] duration-200 hover:-translate-y-px hover:border-border-strong hover:bg-subtle hover:shadow-sm active:translate-y-0 active:scale-[0.99] motion-reduce:hover:translate-y-0"
              >
                <span className="mt-0.5 inline-flex size-5 shrink-0 items-center justify-center text-fg-subtle">
                  {s.connector ? <ConnectorIcon connector={s.connector} className="size-4" /> : i === 0 ? <Sparkles className="size-4" /> : <MessageCircle className="size-4" />}
                </span>
                <span className="flex-1">{s.text}</span>
                <ArrowRight
                  className="mt-0.5 size-4 shrink-0 text-fg-subtle opacity-0 transition-[opacity,transform] duration-200 group-hover:translate-x-0.5 group-hover:opacity-100"
                  aria-hidden="true"
                />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
