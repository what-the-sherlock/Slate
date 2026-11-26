"use client";

import { ArrowUp, Check, CalendarClock, ChevronDown, Layers, Square } from "lucide-react";
import { useLayoutEffect, type FormEvent, type KeyboardEvent, type RefObject } from "react";
import { ConnectorIcon } from "@/components/brand";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Source } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";
import { DATE_PRESETS, MAX_FROM_CHARS, type DatePreset } from "@/lib/search";

export const LIMIT = 4000;
const COUNTER_AT = 3600;
const MAX_HEIGHT = 8 * 24; // 8 lines

/**
 * The prompt composer (UI_DESIGN §4.3, Beautiful UI's prompt bar, sign-in's pill styling).
 * Always pinned to the bottom of the chat column. Enter sends, Shift+Enter adds a line,
 * ↑ in an empty box recalls the last question; IME composition never sends.
 *
 * Send is tinted while there's nothing to send and turns solid with a glow once there is
 * (the sign-in "ready" pattern). While an answer streams, it morphs into Stop.
 */
export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  onRecall,
  streaming,
  error,
  inputRef,
  sources,
  selected,
  onSelected,
  date = "any",
  onDate = () => {},
  from = "",
  onFrom = () => {},
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onRecall: () => string | undefined;
  streaming: boolean;
  error: string | null;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  sources: Source[];
  selected: string[];
  onSelected: (ids: string[]) => void;
  date?: DatePreset;
  onDate?: (d: DatePreset) => void;
  from?: string;
  onFrom?: (f: string) => void;
}) {
  const over = value.length > LIMIT;
  const empty = !value.trim();
  const ready = !empty && !over && !streaming;
  const blocked = empty ? "Type a question first" : over ? `Questions are limited to ${LIMIT.toLocaleString()} characters` : null;

  // Grow with the text (1–8 lines), including when the value changes from outside
  // (recall, rephrase, a failed send handing the question back).
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value, inputRef]);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready) onSubmit();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (ready) onSubmit();
    } else if (e.key === "ArrowUp" && !value) {
      const last = onRecall();
      if (last) {
        e.preventDefault();
        onChange(last);
      }
    }
  }

  return (
    <form onSubmit={submit} className="relative">
      {error ? (
        <p role="alert" className="mb-2 flex items-center gap-1.5 px-1 text-caption text-danger animate-in fade-in slide-in-from-bottom-1 duration-200">
          {error}
        </p>
      ) : null}
      <div
        className={cn(
          "rounded-2xl border bg-bg shadow-[0_1px_2px_rgb(28_25_23/0.04),0_8px_24px_-12px_rgb(28_25_23/0.12)]",
          "transition-[border-color,box-shadow] duration-200 ease-[var(--sl-ease-standard)]",
          "focus-within:border-brand focus-within:shadow-[0_0_0_3px_var(--sl-accent-subtle),0_8px_24px_-12px_rgb(48_80_240/0.25)]",
          over ? "border-danger" : "border-border-strong",
        )}
      >
        <label htmlFor="composer" className="sr-only">
          Ask a question
        </label>
        <textarea
          ref={inputRef}
          id="composer"
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask anything about your sources…"
          aria-describedby="composer-hint"
          className="block max-h-48 w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[15px] leading-6 text-fg transition-[height] duration-100 outline-none placeholder:text-fg-subtle"
        />
        <div className="flex items-center gap-2 px-2.5 pt-1 pb-2.5">
          {sources.length ? <SourceFilter sources={sources} selected={selected} onSelected={onSelected} /> : null}
          {sources.length ? <MoreFilters date={date} onDate={onDate} from={from} onFrom={onFrom} /> : null}
          {value.length >= COUNTER_AT ? (
            <span className={cn("text-data tabular-nums animate-in fade-in duration-200", over ? "font-medium text-danger" : "text-fg-subtle")}>
              {value.length.toLocaleString()} / {LIMIT.toLocaleString()}
            </span>
          ) : null}
          <span id="composer-hint" className="ml-auto hidden text-data text-fg-subtle sm:inline">
            <kbd className="font-sans">↵</kbd> send · <kbd className="font-sans">⇧↵</kbd> new line
          </span>
          {streaming ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  key="stop"
                  type="button"
                  onClick={onStop}
                  aria-label="Stop generating"
                  className="ml-auto inline-flex size-8 shrink-0 items-center justify-center rounded-lg bg-fg text-white shadow-xs transition-[transform,background-color] duration-150 animate-in zoom-in-75 fade-in hover:bg-fg-body active:scale-95 sm:ml-0"
                >
                  <Square className="size-3 fill-current" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">Stop generating</TooltipContent>
            </Tooltip>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                {/* aria-disabled, not disabled: the button stays focusable so the tooltip can say why. */}
                <button
                  key="send"
                  type="submit"
                  aria-label="Send"
                  aria-disabled={!ready}
                  data-state={ready ? "ready" : "idle"}
                  className={cn(
                    "ml-auto inline-flex size-8 shrink-0 items-center justify-center rounded-lg transition-[transform,background-color,box-shadow,color] duration-200 ease-[var(--sl-ease-standard)] animate-in zoom-in-75 fade-in sm:ml-0",
                    ready
                      ? "bg-brand text-white shadow-[0_6px_16px_-8px_rgb(48_80_240/0.9)] hover:bg-brand-strong active:scale-95"
                      : "cursor-not-allowed bg-brand-subtle text-brand/50",
                  )}
                >
                  <ArrowUp className={cn("size-4 transition-transform duration-200", ready && "group-hover:-translate-y-px")} strokeWidth={2.25} />
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">{blocked ?? "Send (Enter)"}</TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>
    </form>
  );
}

/** "Any time ▾": date range and sender (POST /chat `updated_after`, `author`). */
function MoreFilters({ date, onDate, from, onFrom }: { date: DatePreset; onDate: (d: DatePreset) => void; from: string; onFrom: (f: string) => void }) {
  const active = date !== "any" || !!from.trim();
  const label = [date !== "any" ? DATE_PRESETS[date].label : null, from.trim() ? `from ${from.trim()}` : null].filter(Boolean).join(", ") || "Any time";
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex h-7 max-w-[220px] items-center gap-1.5 rounded-full border px-2.5 text-caption transition-colors duration-150",
            active ? "border-brand-border bg-brand-subtle text-brand-strong" : "border-border text-fg-muted hover:border-border-strong hover:bg-subtle hover:text-fg",
          )}
          aria-label={`When and who: ${label}`}
        >
          <CalendarClock className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{label}</span>
          <ChevronDown className="size-3 shrink-0 opacity-70" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" className="w-64">
        <p className="px-2 pt-1.5 pb-1 text-label font-semibold tracking-wider text-fg-subtle uppercase">Updated</p>
        <ul role="radiogroup" aria-label="Updated" className="mb-2">
          {(Object.keys(DATE_PRESETS) as DatePreset[]).map((k) => (
            <li key={k}>
              <button
                type="button"
                role="radio"
                aria-checked={date === k}
                onClick={() => onDate(k)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-caption text-fg hover:bg-muted"
              >
                <span className="inline-flex size-4 items-center justify-center">{date === k ? <Check className="size-3.5 text-brand" /> : null}</span>
                {DATE_PRESETS[k].label}
              </button>
            </li>
          ))}
        </ul>
        <label className="block px-2 pb-2 text-label font-semibold tracking-wider text-fg-subtle uppercase">
          From
          <input
            value={from}
            onChange={(e) => onFrom(e.target.value.slice(0, MAX_FROM_CHARS))}
            placeholder="Name or email"
            className="mt-1 block h-8 w-full rounded-md border border-border-input bg-bg px-2 text-caption font-normal tracking-normal text-fg normal-case outline-none focus:border-brand"
          />
        </label>
      </PopoverContent>
    </Popover>
  );
}

/** "All sources ▾": limit retrieval to some sources (POST /chat `source_ids`). */
function SourceFilter({ sources, selected, onSelected }: { sources: Source[]; selected: string[]; onSelected: (ids: string[]) => void }) {
  const active = selected.length > 0;
  const label = !active ? "All sources" : selected.length === 1 ? (sources.find((s) => s.id === selected[0])?.name ?? "1 source") : `${selected.length} sources`;
  const toggle = (id: string) => onSelected(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex h-7 max-w-[200px] items-center gap-1.5 rounded-full border px-2.5 text-caption transition-colors duration-150",
            active ? "border-brand-border bg-brand-subtle text-brand-strong" : "border-border text-fg-muted hover:border-border-strong hover:bg-subtle hover:text-fg",
          )}
          aria-label={`Search in: ${label}`}
        >
          <Layers className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{label}</span>
          <ChevronDown className="size-3 shrink-0 opacity-70" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" className="w-72">
        <p className="px-2 pt-1.5 pb-1 text-label font-semibold tracking-wider text-fg-subtle uppercase">Search in</p>
        <button
          type="button"
          onClick={() => onSelected([])}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-caption text-fg hover:bg-muted"
        >
          <span className="inline-flex size-4 items-center justify-center">{!active ? <Check className="size-3.5 text-brand" /> : null}</span>
          All sources
        </button>
        <div className="my-1 h-px bg-border" />
        <ul className="max-h-60 overflow-y-auto" role="group" aria-label="Sources">
          {sources.map((s) => {
            const on = selected.includes(s.id);
            return (
              <li key={s.id}>
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={on}
                  onClick={() => toggle(s.id)}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-caption text-fg hover:bg-muted"
                >
                  <span
                    className={cn(
                      "inline-flex size-4 shrink-0 items-center justify-center rounded-xs border transition-colors duration-150",
                      on ? "border-brand bg-brand text-white" : "border-border-strong",
                    )}
                  >
                    {on ? <Check className="size-3 animate-in zoom-in-50 duration-150" strokeWidth={3} /> : null}
                  </span>
                  <ConnectorIcon connector={s.connector} className="size-3.5 shrink-0" />
                  <span className="min-w-0 flex-1 truncate">{s.name}</span>
                  <span className="text-data text-fg-subtle tabular-nums">{s.documents}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
