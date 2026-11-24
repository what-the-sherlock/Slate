"use client";

import { Globe, Lock, Plug, Plus, Users } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
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
  // Home's "Add source" links here with ?add=1: the URL itself opens the dialog.
  const params = useSearchParams();
  const router = useRouter();
  const wantsAdd = canAdd && params.get("add") === "1";
  const dialogOpen = adding || wantsAdd;
  const setDialogOpen = (next: boolean) => {
    setAdding(next);
    if (!next && wantsAdd) router.replace("/sources");
  };
  const syncing = sources.data?.filter((s) => s.status === "syncing" || s.status === "queued").length ?? 0;

  return (
    <>
      <PageHeader
        title="Sources"
        description="Everything Slate can search, and who can see it"
        actions={
          <>
            {syncing ? (
              <span className="inline-flex items-center gap-2 text-caption text-fg-subtle" role="status">
                <span className="relative inline-flex size-1.5">
                  <span className="absolute inset-0 animate-ping rounded-full bg-brand opacity-60 motion-reduce:hidden" />
                  <span className="relative size-1.5 rounded-full bg-brand" />
                </span>
                Processing {syncing} {syncing === 1 ? "source" : "sources"}
              </span>
            ) : null}
            {canAdd ? (
              <Button variant="primary" onClick={() => setAdding(true)}>
                <Plus aria-hidden="true" />
                Add source
              </Button>
            ) : null}
          </>
        }
      />
      <div className="pb-12">
        {sources.isPending ? (
          <TableSkeleton />
        ) : sources.isError ? (
          <p role="alert" className="mx-4 mt-6 rounded-md border border-danger-border bg-danger-subtle px-3 py-2.5 text-caption text-danger sm:mx-8">
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
        <AddSourceDialog open={dialogOpen} onOpenChange={setDialogOpen} canPublish={me.data?.role === "owner" || me.data?.role === "admin"} />
      ) : null}
    </>
  );
}

const TH = "px-4 text-label font-semibold tracking-wider text-fg-subtle uppercase first:pl-4 last:pr-4 sm:first:pl-8 sm:last:pr-8";
const TD = "px-4 first:pl-4 last:pr-4 sm:first:pl-8 sm:last:pr-8";

function SourcesTable({ rows }: { rows: Source[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[680px] border-collapse text-caption">
        <thead>
          <tr className="h-9 border-b border-border bg-subtle text-left">
            <th scope="col" className={TH}>Source</th>
            <th scope="col" className={TH}>Visibility</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={`${TH} text-right`}>Documents</th>
            <th scope="col" className={TH}>Last synced</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const vis = VISIBILITY[s.visibility];
            return (
              <tr key={s.id} className="relative h-12 border-b border-border transition-colors hover:bg-subtle">
                <td className={TD}>
                  <Link href={`/sources/${s.id}`} className="flex items-center gap-3 after:absolute after:inset-0">
                    <LogoTile connector={s.connector} size={28} />
                    <span className="min-w-0">
                      <span className="block truncate text-body font-medium text-fg">{s.name}</span>
                      <span className="block text-data text-fg-subtle capitalize">{s.connector}</span>
                    </span>
                  </Link>
                </td>
                <td className={TD}>
                  <Pill variant="outline" icon={<vis.icon className="size-3" aria-hidden="true" />}>
                    {vis.label}
                  </Pill>
                </td>
                <td className={TD}>
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
                <td className={`${TD} text-right text-fg tabular-nums`}>{formatCount(s.documents)}</td>
                <td className={`${TD} text-fg-subtle`}>
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
    <div className="flex flex-col" aria-busy="true" aria-label="Loading sources">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex h-12 items-center gap-3 border-b border-border px-4 sm:px-8">
          <Skeleton className="size-7 rounded-md" />
          <Skeleton className="h-3.5 w-40" />
          <Skeleton className="ml-auto h-5 w-16" />
        </div>
      ))}
    </div>
  );
}
