import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

/**
 * The panel grammar (docs/UI_DESIGN.md §3.7, ADR-0017).
 *
 *  - Full-bleed cells, never floating cards: no rounded corners, no shadows, no gaps.
 *  - Separation is a 1px hairline drawn by the ROW (bottom) and by each cell (right).
 *  - Eyebrows are PanelLabel: 10px / 600 / uppercase / wide tracking.
 *  - Every figure is tabular-nums so numbers line up.
 *
 * Pages compose these instead of copying class strings, because copied strings drift.
 */

export function PanelLabel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("text-label font-semibold tracking-wider whitespace-nowrap text-fg-subtle uppercase", className)}>
      {children}
    </span>
  );
}

/** A horizontal band of cells. 1 column on mobile; `cols` columns from lg. */
export function PanelRow({
  cols = 1,
  last = false,
  className,
  children,
}: {
  cols?: 1 | 2 | 3 | 4;
  last?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const grid = {
    1: "",
    2: "grid grid-cols-1 lg:grid-cols-2",
    3: "grid grid-cols-1 lg:grid-cols-3",
    4: "grid grid-cols-2 lg:grid-cols-4",
  }[cols];
  return <div className={cn(grid, !last && "border-b border-border", className)}>{children}</div>;
}

/** One cell in a PanelRow. `last` drops its right hairline. */
export function Panel({
  label,
  action,
  span,
  last = false,
  className,
  children,
}: {
  label?: ReactNode;
  action?: ReactNode;
  span?: 2 | 3;
  last?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      className={cn(
        "min-w-0 px-4 py-5 sm:px-8 sm:py-6",
        !last && "border-b border-border lg:border-r lg:border-b-0",
        span === 2 && "lg:col-span-2",
        span === 3 && "lg:col-span-3",
        className,
      )}
    >
      {label || action ? (
        <div className="mb-4 flex h-5 items-center justify-between gap-4">
          {label ? <PanelLabel>{label}</PanelLabel> : <span />}
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}

/** One KPI tile. Use inside <PanelRow cols={4}>; tiles draw their own hairlines. */
export function Kpi({
  label,
  value,
  caption,
  title,
}: {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  title?: string;
}) {
  return (
    <div className="min-w-0 border-r border-b border-border px-4 py-5 even:border-r-0 sm:px-6 lg:border-b-0 lg:even:border-r lg:last:border-r-0 [&:nth-last-child(-n+2)]:border-b-0">
      <PanelLabel>{label}</PanelLabel>
      <div className="mt-2 truncate text-metric font-semibold tracking-tight text-fg tabular-nums" title={title}>
        {value}
      </div>
      {caption ? <div className="mt-1 truncate text-data text-fg-subtle">{caption}</div> : null}
    </div>
  );
}
