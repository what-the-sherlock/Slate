/**
 * Entrance classes, usable from server and client components alike (no "use client"), which is
 * why they live apart from the hooks in motion.ts. Reduced motion turns the entrance off.
 */
export const REVEAL =
  "animate-in fade-in slide-in-from-bottom-2 duration-500 ease-out fill-mode-both motion-reduce:animate-none";

/** Stagger an entrance: style={delay(80)}. */
export function delay(ms: number) {
  return { animationDelay: `${ms}ms` };
}
