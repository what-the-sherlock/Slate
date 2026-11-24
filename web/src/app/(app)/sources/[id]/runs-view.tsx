"use client";

import { ArrowLeft, History } from "lucide-react";
import Link from "next/link";
import { LogoTile } from "@/components/brand";
import { EmptyState } from "@/components/empty-state";
import { Kpi, PanelLabel, PanelRow } from "@/components/panel";
import { Pill, SourceStatusPill, type PillVariant } from "@/components/status-pill";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiProblem, errorMessage } from "@/lib/api/client";
import { useRuns, useSources } from "@/lib/api/hooks";
import { absoluteTime, formatCount, relativeTime } from "@/lib/format";

const RUN_STATUS: Record<string, { variant: PillVariant; label: string }> = {
  succeeded: { variant: "success", label: "Succeeded" },
  partial: { variant: "warning", label: "Partial" },
  failed: { variant: "danger", label: "Failed" },
  running: { variant: "accent", label: "Running" },
  queued: { variant: "neutral", label: "Queued" },
  cancelled: { variant: "outline", label: "Cancelled" },
};

/** Source detail (UI_DESIGN §4.5): header, stats and run history with errors. */
export function RunsView({ sourceId }: { sourceId: string }) {
  const sources = useSources();
  const runs = useRuns(sourceId);
  const source = sources.data?.find((s) => s.id === sourceId);
  const notFound = runs.error instanceof ApiProblem && runs.error.status === 404;

  return (
    <div className="pb-12">
      <header className="border-b border-border px-4 py-3 sm:px-8">
        <Link href="/sources" className="inline-flex items-center gap-1 text-caption text-fg-subtle hover:text-fg">
          <ArrowLeft className="size-3.5" aria-hidden="true" />
          Sources
        </Link>
        {notFound ? null : (
          <div className="mt-2 flex flex-wrap items-center gap-3">
            {source ? <LogoTile connector={source.connector} size={32} /> : <Skeleton className="size-8 rounded-md" />}
            <div className="min-w-0">
              <h1 className="truncate text-page font-semibold tracking-[-0.02em] text-fg">{source?.name ?? " "}</h1>
              <p className="text-caption text-fg-subtle capitalize">{source?.connector}</p>
            </div>
            {source ? (
              <span className="ml-auto">
                <SourceStatusPill status={source.status} />
              </span>
            ) : null}
          </div>
        )}
      </header>

      {notFound ? (
        <EmptyState icon={History} title="This source isn't available">
          It may have been removed, or you may not have access.
        </EmptyState>
      ) : (
        <>
          {source ? (
            <PanelRow cols={4}>
              <Kpi label="Documents" value={formatCount(source.documents)} />
              <Kpi label="Last synced" value={relativeTime(source.last_synced_at)} title={absoluteTime(source.last_synced_at)} />
              <Kpi label="Visibility" value={<span className="capitalize">{source.visibility.replace("_", " ")}</span>} />
              <Kpi label="Data class" value={<span className="capitalize">{source.data_class}</span>} />
            </PanelRow>
          ) : null}

          <div className="flex h-11 items-center border-b border-border px-4 sm:px-8">
            <PanelLabel>Runs</PanelLabel>
          </div>
          {runs.isPending ? (
            <div className="flex flex-col gap-2 px-4 py-4 sm:px-8">
              {[0, 1].map((i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : runs.isError ? (
            <p role="alert" className="px-4 py-4 text-caption text-danger sm:px-8">
              {errorMessage(runs.error)}
            </p>
          ) : runs.data.length === 0 ? (
            <EmptyState icon={History} title="No runs yet">
              Runs appear here when files are processed.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border border-b border-border">
              {runs.data.map((run) => {
                const st = RUN_STATUS[run.status] ?? { variant: "outline" as const, label: run.status };
                return (
                  <li key={run.id} className="px-4 py-3 transition-colors hover:bg-subtle sm:px-8">
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption">
                      <Pill variant={st.variant}>{st.label}</Pill>
                      <span className="font-medium text-fg capitalize">{run.kind}</span>
                      <span className="text-fg-subtle tabular-nums">
                        {run.docs_changed} changed · {run.docs_failed} failed
                      </span>
                      <time className="ml-auto text-fg-subtle" dateTime={run.started_at ?? undefined} title={absoluteTime(run.started_at)}>
                        {relativeTime(run.started_at)}
                      </time>
                    </div>
                    {run.errors.length ? (
                      <ul className="mt-2 flex flex-col gap-1.5">
                        {run.errors.map((e, i) => (
                          <li key={i} className="flex gap-2 rounded-md border border-danger-border bg-danger-subtle px-3 py-2 text-caption text-danger">
                            <span className="font-mono text-data uppercase">{e.stage}</span>
                            <span className="text-fg-body">{e.message}</span>
                            {e.retryable ? <span className="ml-auto text-data text-fg-subtle">Will retry</span> : null}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
