"use client";

import { cn } from "@/lib/utils";
import { Check, ChevronUp, CornerDownLeft, FileText, Lock, Mic, Paperclip, Sparkles } from "lucide-react";
import { ThinkingOrb } from "@/components/thinking-orb";
import { reached, T } from "./timeline";

const MENU = [
  { label: "Handbook.pdf", hint: "Files", locked: false },
  { label: "#hr-policies", hint: "Discord", locked: false },
  { label: "Exec notes", hint: "No access", locked: true },
];

const STEPS: { label: string; aside?: string }[] = [
  { label: "Reading Handbook.pdf" },
  { label: "Checking who can see it" },
  { label: "Ranking passages", aside: "24 found" },
  { label: "Writing the answer" },
];

/** Column 2: type a question, pick a source with @, send, then the orb thinks (UI_DESIGN §4.1). */
export function AskComposer({ ms, paused }: { ms: number; paused: boolean }) {
  return <div className="relative h-[148px]">{reached(ms, T.thinkStart) ? <Thinking ms={ms} paused={paused} /> : <Composer ms={ms} />}</div>;
}

function SourceChip() {
  return (
    <span className="mx-0.5 inline-flex h-[18px] translate-y-px animate-in items-center gap-1 rounded-sm border border-brand-border bg-brand-subtle px-1 align-baseline font-mono text-[10.5px] text-brand duration-200 zoom-in-75">
      <FileText className="size-2.5" strokeWidth={2} />
      Handbook.pdf
    </span>
  );
}

function Composer({ ms }: { ms: number }) {
  const typed = T.question.slice(0, Math.max(0, Math.floor((ms - T.typeStart) / T.typePerChar)));
  const at = ms >= T.atSign && ms < T.chipIn;
  const chip = ms >= T.chipIn;
  const menu = ms >= T.menuOpen && ms < T.chipIn;
  const onRow = ms >= T.pointerOnRow;
  const pressed = ms >= T.click && ms < T.chipIn;
  const ready = ms >= T.sendReady;
  const sent = ms >= T.send;
  const empty = !typed && !chip;

  return (
    <div className="absolute inset-x-0 bottom-0">
      {menu ? (
        <div className="mb-1.5 w-[188px] animate-in rounded-lg border border-border bg-bg p-1 shadow-md duration-200 fade-in slide-in-from-bottom-1">
          {MENU.map((m, i) => (
            <div
              key={m.label}
              className={cn(
                "flex h-[22px] items-center gap-1.5 rounded-sm px-1.5 text-data transition-colors duration-150",
                m.locked ? "text-fg-disabled" : "text-fg",
                i === 0 && onRow && "bg-brand-subtle text-brand",
                i === 0 && pressed && "scale-[0.98]",
              )}
            >
              {m.locked ? <Lock className="size-3" /> : <FileText className="size-3 text-fg-subtle" />}
              <span className="truncate">{m.label}</span>
              <span className="ml-auto text-[10px] text-fg-subtle">{m.hint}</span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="rounded-xl border border-border bg-bg p-2 pl-2.5 shadow-xs">
        <p className="h-5 truncate text-caption text-fg">
          {empty ? <span className="text-fg-subtle">Ask anything</span> : typed}
          {at ? <span className="text-brand">@</span> : null}
          {chip ? <SourceChip /> : null}
          {!sent ? <span className="ml-px inline-block h-3.5 w-px translate-y-0.5 animate-pulse bg-brand" /> : null}
        </p>
        <div className="mt-1.5 flex items-center gap-2.5 text-fg-subtle">
          <Paperclip className="size-3.5" />
          <Mic className="size-3.5" />
          <span
            className={cn(
              "relative ml-auto flex size-6 items-center justify-center rounded-md transition-[background-color,color,transform] duration-200",
              ready ? "bg-brand text-white" : "bg-brand-subtle text-brand",
              sent && "scale-90",
            )}
          >
            {ready && !sent ? <span className="absolute inset-0 animate-ping rounded-md bg-brand/40" /> : null}
            <CornerDownLeft className="relative size-3.5" />
          </span>
        </div>
      </div>

      {/* The pretend pointer that picks a source from the @ menu. */}
      {ms >= T.pointerIn && ms < T.chipIn ? (
        <svg
          viewBox="0 0 12 16"
          className={cn(
            "absolute z-10 h-4 w-3 drop-shadow-sm transition-[left,top,transform] duration-700 ease-[var(--sl-ease-out)]",
            pressed && "scale-90",
          )}
          style={onRow ? { left: 120, top: 10 } : { left: "85%", top: 96 }}
        >
          <path d="M1 1l9.5 9.2H6.2L8.6 15 6.8 15.8 4.4 11 1 13.8Z" fill="var(--sl-fg)" stroke="white" strokeWidth="1" />
        </svg>
      ) : null}
    </div>
  );
}

function Thinking({ ms, paused }: { ms: number; paused: boolean }) {
  const done = reached(ms, T.thoughtDone);
  const shown = ms < 0 ? STEPS.length : Math.min(STEPS.length, Math.floor((ms - T.thinkStart - 200) / T.stepEvery) + 1);
  return (
    <div className="flex h-full flex-col gap-2">
      <p className="ml-auto max-w-[92%] animate-in rounded-lg bg-muted px-2.5 py-1.5 text-caption text-fg duration-300 fade-in slide-in-from-bottom-3">
        {T.question}
        <SourceChip />
      </p>
      <p className="flex h-5 items-center gap-1.5 text-caption text-fg-muted">
        {done ? (
          <>
            <Sparkles className="size-3.5 text-fg-subtle" />
            Thought for 3 seconds
            <ChevronUp className="size-3 text-fg-subtle" />
          </>
        ) : (
          <>
            <ThinkingOrb state="searching" size={20} decorative paused={paused} />
            <span className="animate-pulse">Thinking…</span>
          </>
        )}
      </p>
      <ol className="ml-[9px] flex flex-col gap-1 border-l border-border pl-3">
        {STEPS.slice(0, Math.max(0, shown)).map((s, i) => {
          const complete = done || i < shown - 1;
          return (
            <li key={s.label} className="flex h-[18px] animate-in items-center gap-1.5 text-caption text-fg duration-300 fade-in slide-in-from-bottom-1">
              {complete ? (
                <Check className="size-3 animate-in text-fg-subtle zoom-in-50" strokeWidth={2.25} />
              ) : (
                <span className="size-3 animate-spin rounded-full border border-border-strong border-t-brand" />
              )}
              {s.label}
              {s.aside ? <span className="text-data text-fg-subtle">{s.aside}</span> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
