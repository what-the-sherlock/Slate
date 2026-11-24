"use client";

import { Globe, Lock, Plug, Plus, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { LogoTile } from "@/components/brand";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Pill, SourceStatusPill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { errorMessage } from "@/lib/api/client";
import { useMe, useSources, type Source } from "@/lib/api/hooks";
import { absoluteTime, formatCount, relativeTime } from "@/lib/format";
import { AddSourceDialog } from "./add-source-dialog";

const VISIBILITY = {
  only_me: { icon: Lock, label: "Only me" },
  workspace: { icon: Users, label: "Workspace" },
  public: { icon: Globe, label: "Public" },
  mixed: { icon: Users, label: "Mixed" },
} as const;

export function SourcesView() {
  const me = useMe();
  const sources = useSources();
  const [adding, setAdding] = useState(false);
  const canAdd = !!me.data && !me.data.is_guest && me.data.role !== "viewer";
  const syncing = sources.data?.filter((s) => s.status === "syncing" || s.status === "queued").length ?? 0;

  return (
    <>
      <PageHeader
        title="Sources"
        actions={
          <>
            {syncing ? (
              <span className="inline-flex items-center gap-2 text-[13px] text-fg-subtle" role="status">
                <span className="relative inline-flex size-1.5">
                  <span className="absolute inset-0 animate-ping rounded-full bg-brand opacity-60 motion-reduce:hidden" />
                  <span className="relative size-1.5 rounded-full bg-brand" />
                </span>
                Processing {syncing} {syncing === 1 ? "source" : "sources"}
              </span>
            ) : null}
            {canAdd ? (
              <Button variant="primary" onClick={() => setAdding(true)}>
                <Plus className="size-4" aria-hidden="true" />
                Add source
              </Button>
            ) : null}
          </>
        }
      />
      <div className="px-4 pb-12 sm:px-8">
        {sources.isPending ? (
          <TableSkeleton />
        ) : sources.isError ? (
          <p role="alert" className="rounded-md border border-danger-border bg-danger-subtle px-3.5 py-3 text-[13px] text-danger">
            {errorMessage(sources.error)}{" "}
            <button className="font-medium underline" onClick={() => sources.refetch()}>
              Retry
            </button>
          </p>
        ) : sources.data.length === 0 ? (
          <EmptyState
            icon={Plug}
            title="No sources yet"
            action={
              canAdd ? (
                <Button variant="primary" onClick={() => setAdding(true)}>
                  Add source
                </Button>
              ) : undefined
            }
          >
            {canAdd
              ? "Upload files, or connect GitHub, Discord and Gmail, to start asking questions."
              : "Nothing has been shared with you yet."}
          </EmptyState>
        ) : (
          <SourcesTable rows={sources.data} />
        )}
      </div>
      {canAdd ? (
        <AddSourceDialog open={adding} onOpenChange={setAdding} canPublish={me.data?.role === "owner" || me.data?.role === "admin"} />
      ) : null}
    </>
  );
}

function SourcesTable({ rows }: { rows: Source[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-sm">
        <thead>
          <tr className="h-9 border-b border-border-subtle text-left text-[12px] font-medium text-fg-subtle">
            <th scope="col" className="px-4 font-medium">Source</th>
            <th scope="col" className="px-4 font-medium">Visibility</th>
            <th scope="col" className="px-4 font-medium">Status</th>
            <th scope="col" className="px-4 text-right font-medium">Documents</th>
            <th scope="col" className="px-4 font-medium">Last synced</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const vis = VISIBILITY[s.visibility];
            return (
              <tr key={s.id} className="relative h-14 border-b border-border-subtle transition-colors hover:bg-subtle">
                <td className="px-4">
                  <Link href={`/sources/${s.id}`} className="flex items-center gap-3 after:absolute after:inset-0">
                    <LogoTile connector={s.connector} size={32} />
                    <span className="min-w-0">
                      <span className="block truncate font-medium text-fg">{s.name}</span>
                      <span className="block text-[12px] text-fg-subtle capitalize">{s.connector}</span>
                    </span>
                  </Link>
                </td>
                <td className="px-4">
                  <Pill variant="outline" icon={<vis.icon className="size-3.5" aria-hidden="true" />}>
                    {vis.label}
                  </Pill>
                </td>
                <td className="px-4">
                  {s.last_error ? (
                    <Tooltip>
                      <TooltipTrigger className="relative z-10">
                        <SourceStatusPill status={s.status} />
                      </TooltipTrigger>
                      <TooltipContent>{s.last_error}</TooltipContent>
                    </Tooltip>
                  ) : (
                    <SourceStatusPill status={s.status} />
                  )}
                </td>
                <td className="px-4 text-right text-fg tabular-nums">{formatCount(s.documents)}</td>
                <td className="px-4 text-fg-subtle">
                  <time dateTime={s.last_synced_at ?? undefined} title={absoluteTime(s.last_synced_at)}>
                    {relativeTime(s.last_synced_at)}
                  </time>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading sources">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex h-14 items-center gap-3 border-b border-border-subtle px-4">
          <Skeleton className="size-8 rounded-md" />
          <Skeleton className="h-4 w-40" />
          <Skeleton className="ml-auto h-6 w-20" />
        </div>
      ))}
    </div>
  );
}
