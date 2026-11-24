"use client";

import { ArrowLeft, History } from "lucide-react";
import Link from "next/link";
import { LogoTile } from "@/components/brand";
import { EmptyState } from "@/components/empty-state";
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
    <div className="px-4 pt-6 pb-12 sm:px-8">
      <Link href="/sources" className="inline-flex items-center gap-1.5 text-[13px] text-fg-subtle hover:text-fg">
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        Sources
      </Link>

      {notFound ? (
        <EmptyState icon={History} title="This source isn't available">
          It may have been removed, or you may not have access.
        </EmptyState>
      ) : (
        <>
          <header className="mt-4 flex flex-wrap items-center gap-4">
            {source ? <LogoTile connector={source.connector} /> : <Skeleton className="size-10 rounded-md" />}
            <div className="min-w-0">
              <h1 className="text-[22px] leading-7 font-medium tracking-[-0.03em] text-fg">{source?.name ?? " "}</h1>
              <p className="text-[13px] text-fg-subtle capitalize">{source?.connector}</p>
            </div>
            {source ? <span className="ml-auto"><SourceStatusPill status={source.status} /></span> : null}
          </header>

          {source ? (
            <dl className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Documents" value={formatCount(source.documents)} />
              <Stat label="Last synced" value={relativeTime(source.last_synced_at)} title={absoluteTime(source.last_synced_at)} />
              <Stat label="Visibility" value={source.visibility.replace("_", " ")} />
              <Stat label="Data class" value={source.data_class} />
            </dl>
          ) : null}

          <h2 className="mt-10 mb-3 text-[18px] font-semibold tracking-[-0.025em] text-fg">Runs</h2>
          {runs.isPending ? (
            <div className="flex flex-col gap-2">
              {[0, 1].map((i) => <Skeleton key={i} className="h-14 w-full" />)}
            </div>
          ) : runs.isError ? (
            <p role="alert" className="text-[13px] text-danger">{errorMessage(runs.error)}</p>
          ) : runs.data.length === 0 ? (
            <EmptyState icon={History} title="No runs yet">
              Runs appear here when files are processed.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border-subtle border-y border-border-subtle">
              {runs.data.map((run) => {
                const st = RUN_STATUS[run.status] ?? { variant: "outline" as const, label: run.status };
                return (
                  <li key={run.id} className="py-3">
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                      <Pill variant={st.variant}>{st.label}</Pill>
                      <span className="text-fg capitalize">{run.kind}</span>
                      <span className="text-fg-subtle tabular-nums">
                        {run.docs_changed} changed · {run.docs_failed} failed
                      </span>
                      <time className="ml-auto text-[13px] text-fg-subtle" dateTime={run.started_at ?? undefined} title={absoluteTime(run.started_at)}>
                        {relativeTime(run.started_at)}
                      </time>
                    </div>
                    {run.errors.length ? (
                      <ul className="mt-2 flex flex-col gap-1.5">
                        {run.errors.map((e, i) => (
                          <li key={i} className="flex gap-2 rounded-md border border-danger-border bg-danger-subtle px-3 py-2 text-[13px] text-danger">
                            <span className="font-mono text-[12px] uppercase">{e.stage}</span>
                            <span className="text-fg-body">{e.message}</span>
                            {e.retryable ? <span className="ml-auto text-[12px] text-fg-subtle">Will retry</span> : null}
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

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div>
      <dt className="text-[12px] font-medium text-fg-subtle">{label}</dt>
      <dd className="mt-1 text-[22px] leading-7 font-medium tracking-[-0.03em] text-fg capitalize tabular-nums" title={title}>
        {value}
      </dd>
    </div>
  );
}
