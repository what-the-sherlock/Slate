"use client";

import { cn } from "@/lib/utils";
import { useRef, useState, type ReactNode } from "react";
import { delay } from "@/lib/reveal";
import { useClock, useInView } from "@/lib/motion";
import { AskComposer } from "./ask-composer";
import { CitedAnswer } from "./cited-answer";
import { ConnectMarquee } from "./connect-marquee";
import { STAGE_START, stageOf, TICK, TOTAL } from "./timeline";

const COLUMNS = [
  { title: "Connect anything", body: "Files today; GitHub, Discord and Gmail next, and more to come." },
  { title: "Ask with @", body: "Pick sources with @. Slate only searches what you can see." },
  { title: "Get a cited answer", body: "Every answer shows its sources, and every claim links to its passage." },
] as const;

/**
 * "How it works" (docs/UI_DESIGN.md §4.1): one story across three columns on one clock.
 *  - The whole card pauses while hovered or focused, offscreen, or in a hidden tab.
 *  - Reduced motion: every column shows its finished frame (clock ms = -1).
 *  - Phones show one column at a time; the tabs follow the story and jump to a column on tap.
 *  - Illustrations are aria-hidden; each column has a real heading and a sentence.
 */
export function HowItWorks() {
  const ref = useRef<HTMLElement>(null);
  const inView = useInView(ref);
  const [held, setHeld] = useState(false);
  const paused = held || !inView;
  const { ms, seek } = useClock(TOTAL, TICK, { paused });
  // Under reduced motion the clock is frozen, so the tapped tab decides what phones show.
  const [picked, setPicked] = useState<0 | 1 | 2>(0);
  const stage = ms < 0 ? picked : stageOf(ms);

  const art: ReactNode[] = [
    <ConnectMarquee key="c" ms={ms} paused={paused} />,
    <AskComposer key="a" ms={ms} paused={paused} />,
    <CitedAnswer key="r" ms={ms} />,
  ];

  return (
    <section
      ref={ref}
      aria-labelledby="how-it-works"
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={() => setHeld(false)}
      className="relative w-full max-w-[1060px] translate-y-px animate-in rounded-t-2xl bg-bg pt-12 text-left shadow-[0_-12px_48px_-24px_rgb(16_24_90/0.45)] duration-700 ease-out fade-in fill-mode-both slide-in-from-bottom-6 motion-reduce:animate-none"
      style={delay(320)}
    >
      <div className="absolute inset-x-0 top-0 flex h-12 items-center justify-between border-b border-border px-5 sm:px-6">
        <h2 id="how-it-works" className="text-body font-medium text-fg">
          How it works
        </h2>
        <span className="hidden text-body text-fg-subtle md:inline">Built on free-tier infrastructure</span>
        <div className="flex gap-1 md:hidden" role="group" aria-label="Show step">
          {COLUMNS.map((c, i) => (
            <button
              key={c.title}
              type="button"
              aria-pressed={stage === i}
              aria-label={`Step ${i + 1}: ${c.title}`}
              onClick={() => {
                setPicked(i as 0 | 1 | 2);
                seek(STAGE_START[i] ?? 0);
              }}
              className={cn(
                "flex size-7 items-center justify-center rounded-full font-mono text-caption transition-colors",
                stage === i ? "bg-brand text-white" : "text-fg-muted hover:bg-muted",
              )}
            >
              {i + 1}
            </button>
          ))}
        </div>
      </div>
      <ol className="grid grid-cols-1 md:grid-cols-3 md:divide-x md:divide-border">
        {COLUMNS.map((c, i) => (
          <li key={c.title} className={cn("min-w-0 flex-col gap-2.5 px-5 py-4 sm:px-6", stage === i ? "flex" : "hidden md:flex")}>
            <h3 className="flex items-baseline gap-2.5 text-ui font-medium text-fg">
              <span className="font-mono text-body text-brand">{i + 1}</span>
              {c.title}
            </h3>
            <p className="sr-only">{c.body}</p>
            <div aria-hidden="true" className="min-w-0 pl-5">
              {art[i]}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
