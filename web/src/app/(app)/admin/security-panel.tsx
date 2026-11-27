"use client";

import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { EmptyState } from "@/components/empty-state";
import { Panel, PanelRow } from "@/components/panel";
import { Pill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api/client";
import { useAdminEvents, useAdminFailedLogins, type AdminEventKind } from "@/lib/api/hooks";
import { absoluteTime, relativeTime } from "@/lib/format";
import { BarChart } from "./bar-chart";
import { EVENT_LABEL, EVENT_TONE } from "./labels";

const KINDS = Object.keys(EVENT_LABEL) as AdminEventKind[];

/**
 * Admin › Security: failed sign-ins per day, then the event feed. Events hold what
 * happened, to which account and when; never an IP, device or password (ADR-0042).
 * Kept 90 days.
 */
export function SecurityPanel() {
  const [kind, setKind] = useState<AdminEventKind | null>(null);
  const failed = useAdminFailedLogins();
  const events = useAdminEvents(kind);
  const rows = events.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
    <PanelRow>
      <Panel label="Failed sign-ins, 30 days" last>
        <BarChart points={failed.data?.points} bucket="day" unit={["failed sign-in", "failed sign-ins"]} tone="danger" height={96} />
        {failed.isError ? (
          <p role="alert" className="mt-2 text-caption text-danger">
            {errorMessage(failed.error)}
          </p>
        ) : null}
      </Panel>
    </PanelRow>
    <PanelRow last>
      <Panel
        label="Recent events"
        last
        action={
          <label className="flex items-center gap-2 text-caption text-fg-muted normal-case">
            <span className="sr-only">Event type</span>
            <select
              value={kind ?? ""}
              onChange={(e) => setKind((e.target.value || null) as AdminEventKind | null)}
              className="h-8 rounded-lg border border-input bg-bg px-2 text-caption text-fg outline-none focus-visible:ring-2 focus-visible:ring-brand"
            >
              <option value="">All events</option>
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {EVENT_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {events.isError ? (
          <p role="alert" className="text-caption text-danger">
            {errorMessage(events.error)}{" "}
            <button className="font-medium underline" onClick={() => events.refetch()}>
              Retry
            </button>
          </p>
        ) : events.isPending ? (
          <div className="space-y-2" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState icon={ShieldCheck} title={kind ? "No events of this type" : "No events yet"}>
            {kind ? "Nothing like this in the last 90 days." : "Sign-ups, sign-ins and account changes appear here."}
          </EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-border rounded-lg border border-border">
              {rows.map((e) => (
                <li key={e.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
                  <Pill variant={EVENT_TONE[e.kind]}>{EVENT_LABEL[e.kind]}</Pill>
                  <span className="min-w-0 flex-1 truncate text-body text-fg" title={e.user_email ?? undefined}>
                    {e.user_email ?? <span className="text-fg-subtle">{e.kind === "account_deleted" || e.user_id ? "Deleted account" : "No matching account"}</span>}
                  </span>
                  <time dateTime={e.at} title={absoluteTime(e.at)} className="text-caption text-fg-subtle">
                    {relativeTime(e.at)}
                  </time>
                </li>
              ))}
            </ul>
            {events.hasNextPage ? (
              <div className="flex justify-center pt-4">
                <Button variant="secondary" onClick={() => events.fetchNextPage()} loading={events.isFetchingNextPage}>
                  Load more
                </Button>
              </div>
            ) : null}
          </>
        )}
      </Panel>
    </PanelRow>
    </>
  );
}
