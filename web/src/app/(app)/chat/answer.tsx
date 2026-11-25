"use client";

import { CircleAlert, CircleHelp, Copy, Layers, ThumbsDown, ThumbsUp } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { ThinkingOrb } from "@/components/thinking-orb";
import { Button } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { answerWithFootnotes, citationMarkdown, safeUrl, type SourceOut, type Turn } from "@/lib/chat";
import { cn } from "@/lib/utils";

const REASONS = ["Wrong", "Missing information", "Bad sources", "Other"] as const;

/** One question bar plus its answer block (UI_DESIGN §4.3). */
export function TurnView({
  turn,
  onOpenSource,
  onRetry,
}: {
  turn: Turn;
  onOpenSource: (turn: Turn, n: number | null) => void;
  onRetry: (turn: Turn) => void;
}) {
  const busy = turn.stage === "searching" || turn.stage === "reading" || turn.stage === "streaming";
  const abstained = turn.done?.abstained ?? false;
  return (
    <section className="flex flex-col gap-4" aria-label={`Question: ${turn.question}`}>
      <div className="flex items-start gap-3 border-y border-border bg-subtle px-4 py-3.5">
        <p className="min-w-0 flex-1 text-[15px] font-medium whitespace-pre-wrap text-fg">{turn.question}</p>
        {turn.sources.length && !abstained ? (
          <button
            type="button"
            onClick={() => onOpenSource(turn, null)}
            className="inline-flex shrink-0 items-center gap-1 text-caption text-fg-subtle hover:text-fg"
          >
            <Layers className="size-3.5" aria-hidden="true" />
            {turn.sources.length} sources
          </button>
        ) : null}
      </div>

      {turn.sources.length && !abstained ? (
        <ul className="flex gap-2 overflow-x-auto px-1 pb-1" aria-label="Sources">
          {turn.sources.map((s) => (
            <li key={s.n} className="animate-in fade-in slide-in-from-bottom-1 duration-300">
              <SourceChip source={s} onClick={() => onOpenSource(turn, s.n)} />
            </li>
          ))}
        </ul>
      ) : null}

      <div aria-live="polite" className="sr-only">
        {turn.stage === "searching" ? "Searching your sources" : turn.stage === "reading" ? `Reading ${turn.sources.length} sources` : turn.stage === "done" ? "Answer ready" : ""}
      </div>

      {turn.stage === "searching" || turn.stage === "reading" ? (
        <div className="flex items-center gap-2 px-1 text-caption text-fg-subtle">
          <ThinkingOrb state={turn.stage === "searching" ? "searching" : "connecting"} size={20} label="Working on your answer" />
          {turn.stage === "searching" ? "Searching your sources…" : `Reading ${turn.sources.length} sources…`}
        </div>
      ) : null}

      {abstained ? (
        <Insufficient turn={turn} onOpenSource={onOpenSource} />
      ) : turn.text ? (
        <div aria-busy={busy || undefined} className="prose-answer max-w-[720px] px-1 text-[15px] leading-7 text-fg-body">
          <AnswerMarkdown turn={turn} onCite={(n) => onOpenSource(turn, n)} />
          {turn.stage === "streaming" ? <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-fg align-middle" aria-hidden="true" /> : null}
        </div>
      ) : null}

      {turn.stage === "stopped" ? <p className="px-1 text-caption text-fg-subtle">Stopped.</p> : null}

      {turn.stage === "error" ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-danger/30 bg-danger/5 px-4 py-3 text-caption text-danger">
          <CircleAlert className="size-4 shrink-0" aria-hidden="true" />
          <span className="flex-1">{turn.text ? "The answer stopped before it finished." : (turn.error?.message ?? "Something went wrong.")}</span>
          {turn.error?.requestId ? <code className="font-mono text-data text-fg-subtle">Ref: {turn.error.requestId}</code> : null}
          <Button size="sm" variant="secondary" onClick={() => onRetry(turn)}>
            Retry
          </Button>
        </div>
      ) : null}

      {turn.stage === "done" && !abstained && turn.messageId ? <AnswerFooter turn={turn} /> : null}
    </section>
  );
}

function SourceChip({ source, onClick }: { source: SourceOut; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex h-7 items-center gap-2 rounded-sm bg-brand-subtle py-[3px] pr-2.5 pl-[3px] text-[13px] font-medium text-brand-strong hover:brightness-95"
    >
      <span className="inline-flex size-[22px] items-center justify-center rounded-xs bg-brand text-[12px] font-semibold text-white tabular-nums">{source.n}</span>
      <span className="max-w-[180px] truncate">{source.title}</span>
    </button>
  );
}

/** Markdown without raw HTML; validated [n] become badges, other links must be http(s). */
function AnswerMarkdown({ turn, onCite }: { turn: Turn; onCite: (n: number) => void }) {
  const byN = new Map(turn.sources.map((s) => [s.n, s]));
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      urlTransform={safeUrl}
      components={{
        a({ href, children }) {
          const m = href?.match(/^cite:(\d+)$/);
          if (m) {
            const n = Number(m[1]);
            // Only numbers the server confirmed become badges (citation events).
            if (!turn.cited.includes(n)) return <span>{children}</span>;
            return (
              <button
                type="button"
                onClick={() => onCite(n)}
                aria-label={`Source ${n}: ${byN.get(n)?.title ?? "document"}`}
                className="relative -top-px mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-xs bg-brand px-1 align-middle text-[11px] font-semibold text-white tabular-nums"
              >
                {n}
              </button>
            );
          }
          if (!href) return <span>{children}</span>;
          // Model-written links could be planted by a document: show where they go.
          let host = "";
          try {
            host = new URL(href).hostname;
          } catch {
            return <span>{children}</span>;
          }
          return (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-brand underline underline-offset-2">
              {children}
              <span className="ml-1 text-data text-fg-subtle no-underline">({host})</span>
            </a>
          );
        },
        // No images: an image URL is fetched on render, so a planted ![](https://x/?d=...)
        // could leak other sources' text without a click (security review).
        img({ alt }) {
          return alt ? <span className="text-fg-subtle">[image: {alt}]</span> : null;
        },
        code({ children, className }) {
          const block = /language-/.test(className ?? "");
          return block ? (
            <code className="block overflow-x-auto font-mono text-[13px] leading-5">{children}</code>
          ) : (
            <code className="rounded-xs bg-muted px-1 font-mono text-[0.92em]">{children}</code>
          );
        },
        pre({ children }) {
          return <pre className="my-3 max-h-[400px] overflow-auto rounded-md border border-border bg-subtle px-4 py-3">{children}</pre>;
        },
      }}
    >
      {citationMarkdown(turn.text, turn.cited)}
    </Markdown>
  );
}

function Insufficient({ turn, onOpenSource }: { turn: Turn; onOpenSource: (turn: Turn, n: number) => void }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-subtle px-4 py-4">
      <div className="flex gap-3">
        <CircleHelp className="mt-0.5 size-4 shrink-0 text-fg-muted" aria-hidden="true" />
        <div>
          <p className="text-ui font-semibold text-fg">Not enough evidence to answer this</p>
          <p className="text-caption text-fg-muted">I couldn&apos;t find passages that answer this question in the sources available to you.</p>
        </div>
      </div>
      {turn.sources.length ? (
        <div className="pl-7">
          <p className="mb-1.5 text-data font-medium text-fg-subtle">Closest matches</p>
          <ul className="flex flex-col gap-1">
            {turn.sources.slice(0, 3).map((s) => (
              <li key={s.n}>
                <button type="button" onClick={() => onOpenSource(turn, s.n)} className="text-left text-caption text-fg hover:text-brand">
                  {s.title}
                  {s.heading_path ? <span className="text-fg-subtle"> · {s.heading_path}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex gap-2 pl-7">
        <Button asChild size="sm" variant="ghost">
          <Link href={`/search?q=${encodeURIComponent(turn.question.slice(0, 500))}`}>Search instead</Link>
        </Button>
      </div>
    </div>
  );
}

function AnswerFooter({ turn }: { turn: Turn }) {
  const [rated, setRated] = useState<1 | -1 | null>(null);
  const [asking, setAsking] = useState(false);

  async function send(rating: 1 | -1, reason?: (typeof REASONS)[number]) {
    try {
      unwrap(
        await api.POST("/api/v1/messages/{message_id}/feedback", {
          params: { path: { message_id: turn.messageId! } },
          body: { rating, reason: reason ?? null },
        }),
      );
      setRated(rating);
      setAsking(false);
      toast.success("Thanks, feedback saved");
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1 px-1">
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Copy answer"
        onClick={() => void navigator.clipboard?.writeText(answerWithFootnotes(turn)).then(() => toast.success("Copied"))}
      >
        <Copy />
      </Button>
      <Button variant="ghost" size="icon-sm" aria-label="Good answer" aria-pressed={rated === 1} onClick={() => void send(1)} className={cn(rated === 1 && "text-brand")}>
        <ThumbsUp />
      </Button>
      <Button variant="ghost" size="icon-sm" aria-label="Bad answer" aria-pressed={rated === -1} aria-expanded={asking} onClick={() => setAsking((a) => !a)} className={cn(rated === -1 && "text-danger")}>
        <ThumbsDown />
      </Button>
      {asking ? (
        <div className="flex flex-wrap gap-1" role="group" aria-label="What was wrong?">
          {REASONS.map((r) => (
            <Button key={r} size="sm" variant="secondary" onClick={() => void send(-1, r)}>
              {r}
            </Button>
          ))}
        </div>
      ) : null}
      {turn.done ? (
        <span className="ml-auto text-data text-fg-subtle tabular-nums">
          {(turn.done.latency_ms / 1000).toFixed(1)} s{turn.done.model ? ` · ${turn.done.model}` : ""}
        </span>
      ) : null}
    </div>
  );
}
