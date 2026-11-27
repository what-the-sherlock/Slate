"use client";

import {
  Check,
  CircleAlert,
  CircleHelp,
  Copy,
  Globe,
  Layers,
  Pencil,
  RotateCcw,
  Search,
  Square,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { answerWithFootnotes, isBusy, type SourceOut, type Turn } from "@/lib/chat";
import { retryIn } from "@/lib/chat-errors";
import { useSmoothText } from "@/lib/use-smooth-text";
import { cn } from "@/lib/utils";
import { AnswerMarkdown } from "./markdown";
import { ThinkingStatus } from "./thinking-status";

const REASONS = ["Wrong", "Missing information", "Bad sources", "Other"] as const;

export interface TurnActions {
  onOpenSource: (turn: Turn, n: number | null) => void;
  onRegenerate: (turn: Turn) => void;
  onEdit: (turn: Turn, question: string) => void;
  onRephrase: (question: string) => void;
  onClearFilters?: () => void;
}

/** One exchange: the question bubble, then the answer (UI_DESIGN §4.3). */
export function TurnView({
  turn,
  isLast,
  threadBusy,
  filtersActive,
  actions,
}: {
  turn: Turn;
  isLast: boolean;
  threadBusy: boolean;
  filtersActive: boolean;
  actions: TurnActions;
}) {
  return (
    <section className="flex flex-col gap-5" aria-label={`Question: ${turn.question.slice(0, 120)}`}>
      <UserMessage turn={turn} isLast={isLast} canEdit={!threadBusy} onEdit={actions.onEdit} />
      <AssistantMessage turn={turn} isLast={isLast} threadBusy={threadBusy} filtersActive={filtersActive} actions={actions} />
    </section>
  );
}

// ---------------------------------------------------------------- the question

function UserMessage({ turn, isLast, canEdit, onEdit }: { turn: Turn; isLast: boolean; canEdit: boolean; onEdit: TurnActions["onEdit"] }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(turn.question);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!editing) return;
    const el = box.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [editing]);

  function submit() {
    const q = draft.trim();
    if (!q) return;
    setEditing(false);
    if (q !== turn.question.trim() || turn.stage !== "done") onEdit(turn, q);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      setEditing(false);
      setDraft(turn.question);
    }
  }

  if (editing) {
    return (
      <div className="ml-auto w-full max-w-[85%] rounded-2xl border border-brand bg-bg p-3 shadow-[0_0_0_3px_var(--sl-accent-subtle)] animate-in fade-in zoom-in-[0.99] duration-150">
        <label htmlFor={`edit-${turn.key}`} className="sr-only">
          Edit your question
        </label>
        <textarea
          id={`edit-${turn.key}`}
          ref={box}
          value={draft}
          rows={1}
          maxLength={4000}
          onChange={(e) => {
            setDraft(e.target.value);
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 240)}px`;
          }}
          onKeyDown={onKeyDown}
          className="block w-full resize-none bg-transparent text-[15px] leading-6 text-fg outline-none"
        />
        <div className="mt-2 flex items-center gap-2">
          {!isLast ? <span className="text-data text-fg-subtle">The answers below will be replaced.</span> : null}
          <span className="ml-auto" />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(false);
              setDraft(turn.question);
            }}
          >
            Cancel
          </Button>
          <Button size="sm" variant="primary" onClick={submit} disabled={!draft.trim()}>
            Send
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="group/q flex flex-col items-end gap-1 animate-in fade-in slide-in-from-bottom-1 duration-300 motion-reduce:animate-none">
      <p className="max-w-[85%] rounded-2xl rounded-br-md bg-muted px-4 py-2.5 text-[15px] leading-6 break-words whitespace-pre-wrap text-fg">
        {turn.question}
      </p>
      <div className="flex gap-0.5 opacity-0 transition-opacity duration-150 group-focus-within/q:opacity-100 group-hover/q:opacity-100 max-sm:opacity-100">
        <CopyButton text={turn.question} label="Copy question" />
        {canEdit ? (
          <IconAction label="Edit question" onClick={() => setEditing(true)}>
            <Pencil />
          </IconAction>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- the answer

function AssistantMessage({
  turn,
  isLast,
  threadBusy,
  filtersActive,
  actions,
}: {
  turn: Turn;
  isLast: boolean;
  threadBusy: boolean;
  filtersActive: boolean;
  actions: TurnActions;
}) {
  const busy = isBusy(turn);
  const streaming = turn.stage === "streaming";
  const text = useSmoothText(turn.text, streaming);
  const abstained = turn.done?.abstained ?? false;
  const mode = turn.done?.mode ?? "grounded";
  const general = mode === "general" && !abstained;
  const showSources = turn.sources.length > 0 && !abstained && mode === "grounded";
  const { onOpenSource } = actions;
  const onCite = useCallback((n: number) => onOpenSource(turn, n), [onOpenSource, turn]);
  const canRegenerate = isLast && !threadBusy;

  return (
    <div className="flex flex-col gap-3">
      <div aria-live="polite" className="sr-only">
        {turn.stage === "searching" ? "Thinking" : turn.stage === "reading" ? `Reading ${turn.sources.length} sources` : turn.stage === "done" ? "Answer ready" : ""}
      </div>

      {showSources ? <SourceStrip turn={turn} onOpen={(n) => onOpenSource(turn, n)} /> : null}

      {(turn.stage === "searching" || turn.stage === "reading") && !turn.text ? <ThinkingStatus turn={turn} /> : null}

      {general ? <GeneralLabel /> : null}

      {abstained ? (
        <Insufficient turn={turn} filtersActive={filtersActive} actions={actions} />
      ) : turn.text ? (
        <div aria-busy={busy || undefined} className="prose-answer animate-in fade-in duration-150 motion-reduce:animate-none">
          <AnswerMarkdown text={text} cited={turn.cited} sources={turn.sources} streaming={streaming} onCite={onCite} />
          {streaming ? <span className="sl-caret" aria-hidden="true" /> : null}
        </div>
      ) : null}

      {turn.stage === "stopped" ? (
        <div className="flex flex-wrap items-center gap-2 animate-in fade-in duration-200">
          <span className="inline-flex h-6 items-center gap-1.5 rounded-full border border-border bg-subtle px-2.5 text-data font-medium text-fg-muted">
            <Square className="size-2.5 fill-current" aria-hidden="true" />
            {turn.text ? "Stopped" : "No answer was saved for this question"}
          </span>
          {canRegenerate ? (
            <Button size="sm" variant="ghost" onClick={() => actions.onRegenerate(turn)}>
              <RotateCcw /> Regenerate
            </Button>
          ) : null}
        </div>
      ) : null}

      {turn.stage === "error" ? <ErrorNotice turn={turn} canRetry={!threadBusy} onRetry={() => actions.onRegenerate(turn)} /> : null}

      {turn.stage === "done" && !abstained && turn.messageId ? (
        <AnswerFooter turn={turn} canRegenerate={canRegenerate} onRegenerate={() => actions.onRegenerate(turn)} onOpenSources={() => onOpenSource(turn, null)} />
      ) : null}
    </div>
  );
}

const STRIP_MAX = 4;

/**
 * Cited sources first (they back the answer), then the rest; at most STRIP_MAX chips plus a
 * "+N more" chip that opens the full panel. Chips wrap rather than scroll, so nothing is
 * silently cut off at the edge.
 */
export function stripSources(sources: SourceOut[], cited: number[]): { shown: SourceOut[]; hidden: number } {
  const ordered = [...sources.filter((s) => cited.includes(s.n)), ...sources.filter((s) => !cited.includes(s.n))];
  const max = ordered.length > STRIP_MAX ? STRIP_MAX - 1 : STRIP_MAX;
  return { shown: ordered.slice(0, max), hidden: Math.max(0, ordered.length - max) };
}

function SourceStrip({ turn, onOpen }: { turn: Turn; onOpen: (n: number | null) => void }) {
  const { shown, hidden } = stripSources(turn.sources, turn.cited);
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Sources">
      {shown.map((s, i) => (
        <li
          key={s.n}
          className="max-w-full min-w-0 animate-in fade-in slide-in-from-bottom-1 fill-mode-both duration-300 motion-reduce:animate-none"
          style={{ animationDelay: `${i * 30}ms` }}
        >
          <SourceChip source={s} cited={turn.cited.includes(s.n)} onClick={() => onOpen(s.n)} />
        </li>
      ))}
      {hidden ? (
        <li className="animate-in fade-in fill-mode-both duration-300 motion-reduce:animate-none" style={{ animationDelay: `${shown.length * 30}ms` }}>
          <button
            type="button"
            onClick={() => onOpen(null)}
            className="inline-flex h-7 items-center rounded-md border border-border px-2.5 text-[13px] font-medium text-fg-muted transition-colors duration-200 hover:border-border-strong hover:bg-subtle hover:text-fg"
          >
            +{hidden} more
          </button>
        </li>
      ) : null}
    </ul>
  );
}

function SourceChip({ source, cited, onClick }: { source: SourceOut; cited: boolean; onClick: () => void }) {
  const title = source.title.trim() || "Untitled";
  return (
    <button
      type="button"
      onClick={onClick}
      title={source.heading_path ? `${title} · ${source.heading_path}` : title}
      className={cn(
        "inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border py-[3px] pr-2.5 pl-[3px] text-[13px] font-medium transition-[background-color,border-color,color] duration-200 active:scale-[0.98]",
        cited
          ? "border-brand-border bg-brand-subtle text-brand-strong hover:border-brand"
          : "border-border bg-bg text-fg-muted hover:border-border-strong hover:bg-subtle hover:text-fg",
      )}
    >
      <span
        className={cn(
          "inline-flex h-[20px] min-w-[20px] shrink-0 items-center justify-center rounded-[5px] px-1 text-[11px] font-semibold tabular-nums",
          cited ? "bg-brand text-white" : "bg-muted text-fg-muted",
        )}
      >
        {source.n}
      </span>
      <span className="max-w-[200px] min-w-0 truncate">{title}</span>
    </button>
  );
}

function GeneralLabel() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="inline-flex h-6 w-fit cursor-default items-center gap-1.5 rounded-full border border-border bg-subtle px-2.5 text-data font-medium text-fg-muted animate-in fade-in duration-200"
        >
          <Globe className="size-3" aria-hidden="true" />
          General answer · not from your sources
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-[260px] text-left">
        Nothing in your sources answered this, so Slate answered from general knowledge. It has no citations: check anything important.
      </TooltipContent>
    </Tooltip>
  );
}

/** A designed outcome, not an error. Same wording for everyone: never hints at hidden documents. */
function Insufficient({ turn, filtersActive, actions }: { turn: Turn; filtersActive: boolean; actions: TurnActions }) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-subtle px-4 py-4 animate-in fade-in slide-in-from-bottom-1 duration-300 motion-reduce:animate-none">
      <div className="flex gap-3">
        <CircleHelp className="mt-0.5 size-4 shrink-0 text-fg-muted" aria-hidden="true" />
        <div>
          <p className="text-ui font-semibold text-fg">Not enough evidence to answer this</p>
          <p className="text-body text-fg-muted">I couldn&apos;t find passages that answer this question in the sources available to you.</p>
        </div>
      </div>
      {turn.sources.length ? (
        <div className="pl-7">
          <p className="mb-1.5 text-data font-medium text-fg-subtle">Closest matches</p>
          <ul className="flex flex-col gap-1">
            {turn.sources.slice(0, 3).map((s) => (
              <li key={s.n}>
                <button type="button" onClick={() => actions.onOpenSource(turn, s.n)} className="text-left text-caption text-fg transition-colors hover:text-brand">
                  {s.title}
                  {s.heading_path ? <span className="text-fg-subtle"> · {s.heading_path}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-wrap gap-1.5 pl-6">
        <Button size="sm" variant="ghost" onClick={() => actions.onRephrase(turn.question)}>
          <Pencil /> Rephrase
        </Button>
        <Button asChild size="sm" variant="ghost">
          <Link href={`/search?q=${encodeURIComponent(turn.question.slice(0, 500))}`}>
            <Search /> Search instead
          </Link>
        </Button>
        {filtersActive && actions.onClearFilters ? (
          <Button size="sm" variant="ghost" onClick={actions.onClearFilters}>
            Clear filters
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function ErrorNotice({ turn, canRetry, onRetry }: { turn: Turn; canRetry: boolean; onRetry: () => void }) {
  const err = turn.error;
  const [now, setNow] = useState(() => Date.now());
  const [since] = useState(() => Date.now());
  const wait = retryIn(err?.retryAfterS, since, now);
  useEffect(() => {
    if (!err?.retryAfterS) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [err?.retryAfterS]);
  return (
    <div role="alert" className="flex flex-col gap-2 rounded-lg border border-danger-border bg-danger-subtle/60 px-4 py-3 animate-in fade-in slide-in-from-bottom-1 duration-300 motion-reduce:animate-none">
      <div className="flex items-start gap-3">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-ui font-semibold text-fg">{err?.title ?? "Something went wrong"}</p>
          <p className="text-body text-fg-muted">{err?.detail ?? "Try again."}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={onRetry} disabled={!canRetry || !!wait}>
          <RotateCcw /> {wait ?? "Try again"}
        </Button>
      </div>
      {err?.requestId ? (
        <details className="pl-7 text-data text-fg-subtle">
          <summary className="w-fit cursor-pointer select-none hover:text-fg-muted">Details</summary>
          <p className="mt-1">
            Reference <code className="rounded-xs bg-bg px-1 font-mono select-all">{err.requestId}</code>
          </p>
        </details>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- actions

function AnswerFooter({
  turn,
  canRegenerate,
  onRegenerate,
  onOpenSources,
}: {
  turn: Turn;
  canRegenerate: boolean;
  onRegenerate: () => void;
  onOpenSources: () => void;
}) {
  const [rated, setRated] = useState<1 | -1 | null>(null);
  const mode = turn.done?.mode ?? "grounded";
  const seconds = turn.done ? (turn.done.latency_ms / 1000).toFixed(1) : null;
  return (
    <div className="-ml-1.5 flex flex-wrap items-center gap-0.5 animate-in fade-in duration-300 motion-reduce:animate-none">
      <CopyButton text={answerWithFootnotes(turn)} label="Copy answer" />
      {canRegenerate ? (
        <IconAction label="Regenerate" onClick={onRegenerate}>
          <RotateCcw />
        </IconAction>
      ) : null}
      <Feedback messageId={turn.messageId!} rated={rated} onRated={setRated} />
      <span className="ml-auto flex items-center gap-2 text-data text-fg-subtle tabular-nums">
        {mode === "grounded" && turn.sources.length ? (
          <button type="button" onClick={onOpenSources} className="inline-flex items-center gap-1 rounded-sm px-1 transition-colors hover:text-fg">
            <Layers className="size-3" aria-hidden="true" />
            {turn.sources.length} source{turn.sources.length === 1 ? "" : "s"}
          </button>
        ) : null}
        {seconds ? <span>{seconds} s</span> : null}
        {turn.done?.model ? <span className="max-sm:hidden">{turn.done.model}</span> : null}
      </span>
    </div>
  );
}

function Feedback({ messageId, rated, onRated }: { messageId: string; rated: 1 | -1 | null; onRated: (r: 1 | -1) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<(typeof REASONS)[number] | null>(null);
  const [comment, setComment] = useState("");
  const [sending, setSending] = useState(false);

  async function send(rating: 1 | -1) {
    setSending(true);
    try {
      unwrap(
        await api.POST("/api/v1/messages/{message_id}/feedback", {
          params: { path: { message_id: messageId } },
          body: { rating, reason: rating === -1 ? reason : null, comment: rating === -1 && comment.trim() ? comment.trim() : null },
        }),
      );
      onRated(rating);
      setOpen(false);
      toast.success("Thanks, feedback saved");
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <IconAction label="Good answer" pressed={rated === 1} onClick={() => void send(1)} className={cn(rated === 1 && "text-brand")}>
        <ThumbsUp className={cn(rated === 1 && "fill-current animate-in zoom-in-75 duration-200")} />
      </IconAction>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Bad answer"
            aria-pressed={rated === -1}
            className={cn(rated === -1 && "text-danger")}
          >
            <ThumbsDown className={cn(rated === -1 && "fill-current")} />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-80 p-3">
          <p className="mb-2 text-caption font-semibold text-fg">What was wrong?</p>
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Reason">
            {REASONS.map((r) => (
              <button
                key={r}
                type="button"
                role="radio"
                aria-checked={reason === r}
                onClick={() => setReason(r)}
                className={cn(
                  "h-7 rounded-full border px-2.5 text-caption transition-colors duration-150",
                  reason === r ? "border-brand bg-brand-subtle text-brand-strong" : "border-border text-fg-muted hover:border-border-strong hover:text-fg",
                )}
              >
                {r}
              </button>
            ))}
          </div>
          <label htmlFor={`fb-${messageId}`} className="mt-3 mb-1 block text-data text-fg-subtle">
            Anything else? (optional)
          </label>
          <textarea
            id={`fb-${messageId}`}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            maxLength={1000}
            rows={2}
            className="block w-full resize-none rounded-md border border-border bg-bg px-2.5 py-1.5 text-caption text-fg outline-none transition-[border-color,box-shadow] focus:border-brand focus:shadow-[0_0_0_3px_var(--sl-accent-subtle)]"
          />
          <div className="mt-3 flex justify-end gap-1.5">
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" loading={sending} onClick={() => void send(-1)}>
              Send feedback
            </Button>
          </div>
        </PopoverContent>
      </Popover>
    </>
  );
}

function IconAction({
  label,
  onClick,
  pressed,
  className,
  children,
}: {
  label: string;
  onClick: () => void;
  pressed?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} aria-pressed={pressed} onClick={onClick} className={className}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

/** Copy, with the icon morphing to a check for 1.5 s (UI_DESIGN §2.7 "copy confirm"). */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <IconAction
      label={copied ? "Copied" : label}
      onClick={() =>
        void navigator.clipboard?.writeText(text).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          },
          () => toast.error("Couldn't copy. Select the text and copy it instead."),
        )
      }
    >
      {copied ? <Check className="text-success animate-in zoom-in-50 duration-150" /> : <Copy />}
    </IconAction>
  );
}
