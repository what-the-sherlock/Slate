import { cn } from "@/lib/utils";
import { Check, CircleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { Spinner } from "@/components/ui/button";

// Status pills per docs/UI_DESIGN.md §3.3: always text, never colour alone.
const VARIANTS = {
  accent: "bg-brand-subtle text-brand",
  success: "bg-success-subtle text-success",
  neutral: "bg-muted text-fg-muted",
  warning: "bg-warning-subtle text-warning",
  danger: "bg-danger-subtle text-danger",
  outline: "border border-border text-fg-muted",
} as const;

export type PillVariant = keyof typeof VARIANTS;

export function Pill({ variant, icon, children }: { variant: PillVariant; icon?: ReactNode; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-sm px-1.5 text-data font-medium whitespace-nowrap",
        VARIANTS[variant],
      )}
    >
      {icon}
      {children}
    </span>
  );
}

const SOURCE_STATUS: Record<string, { variant: PillVariant; label: string }> = {
  synced: { variant: "success", label: "Synced" },
  syncing: { variant: "accent", label: "Syncing" },
  queued: { variant: "neutral", label: "Queued" },
  retrying: { variant: "warning", label: "Retrying" },
  waiting_for_worker: { variant: "warning", label: "Waiting for worker" },
  empty: { variant: "warning", label: "Nothing indexed" },
  failed: { variant: "danger", label: "Failed" },
  never: { variant: "outline", label: "Never synced" },
  paused: { variant: "outline", label: "Paused" },
  partial: { variant: "warning", label: "Partial" },
};

export function SourceStatusPill({ status }: { status: string }) {
  const s = SOURCE_STATUS[status] ?? { variant: "outline" as const, label: status };
  const icon =
    status === "synced" ? (
      <Check className="size-3" aria-hidden="true" />
    ) : status === "syncing" ? (
      <Spinner className="size-2.5" />
    ) : status === "failed" || status === "waiting_for_worker" || status === "empty" ? (
      <CircleAlert className="size-3" aria-hidden="true" />
    ) : null;
  return (
    <Pill variant={s.variant} icon={icon}>
      {s.label}
    </Pill>
  );
}
