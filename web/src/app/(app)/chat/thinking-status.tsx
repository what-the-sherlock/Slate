"use client";

import { useEffect, useState } from "react";
import { ThinkingOrb } from "@/components/thinking-orb";
import type { Turn } from "@/lib/chat";

/**
 * The "AI is working" row (UI_DESIGN §4.3, §5): a 20 px orb and a shimmering stage label,
 * in a fixed 28 px row so nothing below it shifts. It unmounts when the first token
 * arrives; the answer text fades in where it stood, which reads as one continuous motion.
 *
 *   sent, before `sources`  ->  orb "searching",  "Thinking…"
 *   `sources` received      ->  orb "connecting", "Reading 5 sources…"
 *   no token after 20 s     ->  "Still working… this is taking longer than usual."
 *
 * The orb stays the one sign of model work: never used for page loads or syncs.
 */
export function ThinkingStatus({ turn }: { turn: Turn }) {
  const elapsed = useElapsed(turn.startedAt);
  const reading = turn.stage === "reading";
  const label = turn.slow
    ? "Still working… this is taking longer than usual."
    : reading
      ? `Reading ${turn.sources.length} source${turn.sources.length === 1 ? "" : "s"}…`
      : "Thinking…";
  return (
    <div className="flex h-7 items-center gap-2.5 animate-in fade-in duration-200 motion-reduce:animate-none">
      <ThinkingOrb state={reading ? "connecting" : "searching"} size={20} decorative />
      <span className="sl-shimmer text-body font-medium">{label}</span>
      {elapsed >= 3 ? <span className="text-data text-fg-subtle tabular-nums animate-in fade-in duration-300">{elapsed}s</span> : null}
    </div>
  );
}

function useElapsed(since: number | undefined): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return since ? Math.max(0, Math.floor((now - since) / 1000)) : 0;
}
