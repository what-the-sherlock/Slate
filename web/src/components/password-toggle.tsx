"use client";

import { Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Show/hide button for a password field. type="button" so it never submits the form;
 * aria-pressed plus a changing label tell screen readers the current state.
 */
export function PasswordToggle({
  shown,
  onToggle,
  controls,
  className,
}: {
  shown: boolean;
  onToggle: () => void;
  /** id of the input it controls */
  controls: string;
  className?: string;
}) {
  const Icon = shown ? EyeOff : Eye;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={shown}
      aria-controls={controls}
      aria-label={shown ? "Hide password" : "Show password"}
      title={shown ? "Hide password" : "Show password"}
      className={cn(
        "flex size-8 shrink-0 items-center justify-center rounded-md text-fg-subtle transition-colors hover:bg-muted hover:text-fg",
        className,
      )}
    >
      <Icon className="size-4" strokeWidth={1.75} aria-hidden="true" />
    </button>
  );
}
