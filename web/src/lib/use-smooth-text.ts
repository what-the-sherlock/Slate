"use client";

import { useEffect, useState } from "react";
import { usePrefersReducedMotion } from "@/lib/motion";

/**
 * Reveal streamed text at a steady pace (UI_DESIGN §2.7 "streaming text").
 *
 * Tokens arrive in bursts (network chunks, model hiccups). Rendering each burst as it lands
 * makes text jump; this hook shows the received text through a requestAnimationFrame loop
 * instead, revealing faster the further it falls behind, so the visible text never lags
 * more than ~1/4 s yet flows word by word. When the stream ends (done, stop, error) or the
 * user prefers reduced motion, the full text shows at once.
 */
const CATCH_UP_MS = 250;

/** Characters to show next frame: at least one, and enough to clear the backlog in ~250 ms. */
export function nextLength(shown: number, target: number, elapsedMs: number): number {
  const backlog = target - shown;
  if (backlog <= 0) return target;
  return Math.min(target, shown + Math.max(1, Math.ceil((backlog * elapsedMs) / CATCH_UP_MS)));
}

export function useSmoothText(text: string, streaming: boolean): string {
  const reduced = usePrefersReducedMotion();
  const animate = streaming && !reduced;
  const target = text.length;
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (!animate) return;
    let frame = 0;
    let last = performance.now();
    const tick = (time: number) => {
      const elapsed = time - last;
      last = time;
      // Shrunk (a regenerated answer reuses the slot): start again from nothing.
      setShown((s) => nextLength(s > target ? 0 : s, target, elapsed));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [animate, target]);

  return animate ? text.slice(0, Math.min(shown, target)) : text;
}
