"use client";

import { cn } from "@/lib/utils";
import { useId, useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { bucketLabel, niceMax } from "./labels";

type Point = { start: string; count: number };

/**
 * A single-series column chart (dataviz guidance): thin columns with 2px gaps, 4px rounded
 * data-ends on the baseline, a recessive grid, a hover tooltip per column, and a table view
 * so the numbers never depend on seeing the bars. One series, so no legend: the panel
 * label names it. Plain HTML/CSS, no chart library (UI_DESIGN §12).
 */
export function BarChart({
  points,
  bucket,
  unit,
  tone = "brand",
  loading = false,
  height = 160,
}: {
  points: Point[] | undefined;
  bucket: "day" | "week" | "month";
  unit: [singular: string, plural: string];
  tone?: "brand" | "danger";
  loading?: boolean;
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const tableId = useId();

  if (!points) return <Skeleton className="w-full" style={{ height: height + 24 }} />;

  const max = niceMax(Math.max(0, ...points.map((p) => p.count)));
  const total = points.reduce((n, p) => n + p.count, 0);
  const noun = (n: number) => (n === 1 ? unit[0] : unit[1]);
  const ticks = [0, Math.round(max / 2), max];
  const labelEvery = Math.max(1, Math.ceil(points.length / 6));
  const active = hover !== null ? points[hover] : null;

  return (
    <div className={cn("transition-opacity", loading && "opacity-60")} aria-busy={loading}>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <p className="text-caption text-fg-muted">
          <span className="font-semibold text-fg tabular-nums">{total.toLocaleString("en-US")}</span> {noun(total)} in this period
        </p>
        <button
          type="button"
          onClick={() => setAsTable((v) => !v)}
          aria-expanded={asTable}
          aria-controls={tableId}
          className="rounded-sm text-data font-medium text-fg-muted underline-offset-2 outline-none hover:text-fg hover:underline focus-visible:ring-2 focus-visible:ring-brand"
        >
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>

      {asTable ? (
        <div id={tableId} className="max-h-[260px] overflow-y-auto rounded-md border border-border">
          <table className="w-full border-collapse text-caption">
            <thead className="sticky top-0 bg-subtle">
              <tr className="h-8 border-b border-border text-left">
                <th scope="col" className="px-3 text-label font-semibold tracking-wider text-fg-subtle uppercase">Period</th>
                <th scope="col" className="px-3 text-right text-label font-semibold tracking-wider text-fg-subtle uppercase">Count</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.start} className="h-8 border-b border-border-subtle last:border-b-0">
                  <td className="px-3 text-fg-body">{bucketLabel(p.start, bucket, true)}</td>
                  <td className="px-3 text-right text-fg tabular-nums">{p.count.toLocaleString("en-US")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div id={tableId} className="flex gap-2">
          {/* y-axis: recessive, three ticks */}
          <div className="relative w-6 shrink-0 text-right text-label text-fg-subtle tabular-nums" style={{ height }} aria-hidden="true">
            {ticks.map((t) => (
              <span key={t} className="absolute right-0 -translate-y-1/2" style={{ top: height - (t / max) * height }}>
                {t}
              </span>
            ))}
          </div>
          <div className="relative min-w-0 flex-1">
            <div className="relative" style={{ height }} role="img" aria-label={`${total} ${noun(total)}; open the table for each period's figure.`} onMouseLeave={() => setHover(null)}>
              {ticks.map((t) => (
                <div key={t} className={cn("absolute inset-x-0 h-px", t === 0 ? "bg-border-strong" : "bg-border-subtle")} style={{ top: height - (t / max) * height }} aria-hidden="true" />
              ))}
              <div className="absolute inset-0 flex items-end gap-[2px]">
                {points.map((p, i) => (
                  <div key={p.start} className="flex h-full min-w-0 flex-1 items-end" onMouseEnter={() => setHover(i)}>
                    <div
                      className={cn(
                        "w-full rounded-t-[4px] transition-colors",
                        tone === "brand" ? "bg-brand" : "bg-danger",
                        hover !== null && hover !== i && "opacity-40",
                      )}
                      style={{ height: p.count === 0 ? 0 : Math.max(2, (p.count / max) * height) }}
                    />
                  </div>
                ))}
              </div>
              {active && hover !== null ? (
                <div
                  className="pointer-events-none absolute top-0 z-10 -translate-x-1/2 rounded-md border border-border bg-bg px-2.5 py-1.5 text-data whitespace-nowrap shadow-sm"
                  style={{ left: `${((hover + 0.5) / points.length) * 100}%` }}
                  role="status"
                >
                  <span className="block text-fg-subtle">{bucketLabel(active.start, bucket, true)}</span>
                  <span className="font-semibold text-fg tabular-nums">
                    {active.count.toLocaleString("en-US")} {noun(active.count)}
                  </span>
                </div>
              ) : null}
            </div>
            <div className="relative mt-1.5 h-4 text-label text-fg-subtle" aria-hidden="true">
              {points.map((p, i) =>
                i % labelEvery === 0 ? (
                  <span key={p.start} className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: `${((i + 0.5) / points.length) * 100}%` }}>
                    {bucketLabel(p.start, bucket)}
                  </span>
                ) : null,
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
