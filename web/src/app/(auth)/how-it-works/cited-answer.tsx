"use client";

import { cn } from "@/lib/utils";
import { Copy, CornerDownRight, FileText, RotateCcw, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { LogoTile } from "@/components/brand";
import { reached, T } from "./timeline";

const WORDS = "New parents get 16 weeks of paid leave, usable in blocks over the first year.".split(" ");
const SOURCES = ["upload", "discord", "github"] as const;

/** Column 3: the sources Slate used, then the answer streaming in with its citation. */
export function CitedAnswer({ ms }: { ms: number }) {
  const [peek, setPeek] = useState(false);
  const words = ms < 0 ? WORDS.length : Math.max(0, Math.min(WORDS.length, Math.floor((ms - T.streamStart) / T.perWord)));
  const streaming = ms >= T.streamStart && words < WORDS.length;

  return (
    <div className="relative flex h-[148px] flex-col gap-2">
      <div className={cn("flex h-5 items-center gap-2 transition-opacity duration-300", reached(ms, T.sourcesIn) ? "opacity-100" : "opacity-0")}>
        <span className="flex -space-x-1.5">
          {SOURCES.map((s, i) => (
            <span
              key={s}
              className={cn("rounded-md ring-2 ring-bg", reached(ms, T.sourcesIn) && "animate-in duration-300 fade-in slide-in-from-left-2")}
              style={{ animationDelay: `${i * 90}ms` }}
            >
              <LogoTile connector={s} size={18} />
            </span>
          ))}
        </span>
        <span className="text-caption text-fg-muted">3 sources</span>
      </div>

      <p className="min-h-[54px] text-caption leading-[18px] text-fg">
        {WORDS.slice(0, words).map((w, i) => (
          <span key={i} className="animate-in duration-150 fade-in">
            {i === 3 || i === 4 ? <strong className="font-semibold">{w}</strong> : w}{" "}
          </span>
        ))}
        {reached(ms, T.chipPop) ? (
          <span
            onMouseEnter={() => setPeek(true)}
            onMouseLeave={() => setPeek(false)}
            className="inline-flex h-[18px] cursor-default animate-in items-center gap-1 rounded-sm border border-border bg-subtle px-1 align-baseline font-mono text-[10.5px] text-fg-muted duration-300 ease-[cubic-bezier(0.34,1.56,0.64,1)] zoom-in-50 hover:border-brand-border hover:text-brand"
          >
            <FileText className="size-2.5" />
            Handbook.pdf
          </span>
        ) : null}
        {streaming ? <span className="ml-0.5 inline-block h-3 w-1.5 translate-y-0.5 animate-pulse rounded-[1px] bg-fg" /> : null}
      </p>

      <div className={cn("flex items-center gap-2.5 text-fg-subtle transition-opacity duration-300", reached(ms, T.actionsIn) ? "opacity-100" : "opacity-0")}>
        <Copy className="size-3.5" />
        <RotateCcw className="size-3.5" />
        <ThumbsUp className="size-3.5" />
        <ThumbsDown className="size-3.5" />
      </div>

      <p
        className={cn(
          "flex items-center gap-1.5 border-t border-border pt-1.5 text-caption text-fg-muted transition-[opacity,transform] duration-300",
          reached(ms, T.followUpIn) ? "translate-y-0 opacity-100" : "translate-y-1 opacity-0",
        )}
      >
        <CornerDownRight className="size-3 text-fg-subtle" />
        How do I request it?
      </p>

      {/* Hovering the citation previews the passage it points to. */}
      {peek ? (
        <div className="absolute inset-x-0 top-7 z-10 animate-in rounded-lg border border-border bg-bg p-2.5 shadow-md duration-150 fade-in zoom-in-95">
          <p className="mb-1 flex items-center gap-1.5 text-data text-fg-subtle">
            <LogoTile connector="upload" size={14} />
            Handbook.pdf · p. 4
          </p>
          <p className="text-caption leading-snug text-fg-muted">
            …eligible employees receive <span className="sl-sweep rounded-xs text-fg">sixteen (16) weeks of paid parental leave</span>,
            usable in blocks…
          </p>
        </div>
      ) : null}
    </div>
  );
}
