"use client";

import { ThinkingOrb as Orb, type OrbState } from "thinking-orbs";

/**
 * The one place that imports `thinking-orbs` (docs/UI_DESIGN.md §5), so it can be swapped.
 * Only the two tuned sizes exist; never scale with CSS. The orb means "the AI is working",
 * so it's never used for page loads, syncs or uploads.
 */
export function ThinkingOrb({
  state,
  size = 20,
  label,
  decorative = false,
  paused = false,
  className,
}: {
  state: OrbState;
  size?: 20 | 64;
  label?: string;
  /** Inside an aria-hidden illustration: drop the role and label. */
  decorative?: boolean;
  paused?: boolean;
  className?: string;
}) {
  return (
    <Orb
      state={state}
      size={size}
      theme="light"
      paused={paused}
      className={className}
      {...(decorative ? { role: "presentation", "aria-hidden": true, "aria-label": undefined } : label ? { "aria-label": label } : {})}
    />
  );
}
