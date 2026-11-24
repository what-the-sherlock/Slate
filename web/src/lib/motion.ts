"use client";

/**
 * Motion helpers for the auth hero (docs/UI_DESIGN.md §2.7, ADR-0017).
 *
 * An animation loop here is just a number that changes over time ("which step is live");
 * components render from it and CSS transitions do the smoothing. No animation library.
 *
 * Rules every helper follows:
 *  - prefers-reduced-motion: loops stop and values jump to their finished state.
 *  - Loops stop while the tab is hidden, and while the caller pauses them (hover, offscreen).
 *  - The server snapshot is "reduced", so nothing moves before hydration.
 */
import { useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";

export { delay, REVEAL } from "./reveal";

const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeReduced(cb: () => void) {
  const mql = window.matchMedia?.(REDUCED_QUERY);
  mql?.addEventListener("change", cb);
  return () => mql?.removeEventListener("change", cb);
}

export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReduced,
    () => window.matchMedia?.(REDUCED_QUERY).matches ?? false,
    () => true,
  );
}

function subscribeVisibility(cb: () => void) {
  document.addEventListener("visibilitychange", cb);
  return () => document.removeEventListener("visibilitychange", cb);
}

export function usePageVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== "hidden",
    () => false,
  );
}

/**
 * The step (0..count-1) that is live now, advancing every `ms`. Returns -1 when motion is
 * reduced, so callers render their finished state. Holds its step while paused or hidden.
 */
export function useCycle(count: number, ms: number, { paused = false }: { paused?: boolean } = {}): number {
  const reduced = usePrefersReducedMotion();
  const visible = usePageVisible();
  const [index, setIndex] = useState(0);
  const running = !reduced && visible && !paused && count > 1;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setIndex((i) => (i + 1) % count), ms);
    return () => clearInterval(timer);
  }, [running, count, ms]);
  return reduced ? -1 : index % count;
}

/**
 * A looping millisecond timeline that one parent owns and several children read, so their
 * animations play as one story. Advances in `tickMs` steps and wraps at `totalMs`. `ms` is -1
 * when motion is reduced (render the finished frame). `seek` jumps to a moment.
 */
export function useClock(
  totalMs: number,
  tickMs: number,
  { paused = false }: { paused?: boolean } = {},
): { ms: number; seek: (ms: number) => void } {
  const reduced = usePrefersReducedMotion();
  const visible = usePageVisible();
  const [ticks, setTicks] = useState(0);
  const steps = Math.max(1, Math.round(totalMs / tickMs));
  const running = !reduced && visible && !paused;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setTicks((t) => (t + 1) % steps), tickMs);
    return () => clearInterval(timer);
  }, [running, steps, tickMs]);
  return {
    ms: reduced ? -1 : (ticks % steps) * tickMs,
    seek: (ms: number) => setTicks(Math.max(0, Math.round(ms / tickMs)) % steps),
  };
}

/** `text`, typed out one character every `msPerChar`. The full text when motion is reduced. */
export function useTypewriter(text: string, msPerChar = 40): string {
  const reduced = usePrefersReducedMotion();
  const visible = usePageVisible();
  const [state, setState] = useState({ text, n: 0 });
  const n = state.text === text ? state.n : 0;
  const running = !reduced && visible && n < text.length;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      setState((prev) => {
        const current = prev.text === text ? prev.n : 0;
        return current >= text.length ? prev : { text, n: current + 1 };
      });
    }, msPerChar);
    return () => clearInterval(timer);
  }, [running, text, msPerChar]);
  return reduced ? text : text.slice(0, n);
}

/** Counts from the last shown value to `target` over `ms`, eased out. `target` when reduced. */
export function useCountUp(target: number, ms = 700): number {
  const reduced = usePrefersReducedMotion();
  const [value, setValue] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    if (reduced) return;
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / ms);
      const v = Math.round(origin + (target - origin) * (1 - Math.pow(1 - p, 3)));
      from.current = v;
      setValue(v);
      if (p < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, ms, reduced]);
  return reduced ? target : value;
}

/** Whether `ref` is on screen. True where IntersectionObserver doesn't exist (tests, old browsers). */
export function useInView(ref: RefObject<Element | null>): boolean {
  const [inView, setInView] = useState(() => typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([entry]) => setInView(entry?.isIntersecting ?? false));
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return inView;
}
