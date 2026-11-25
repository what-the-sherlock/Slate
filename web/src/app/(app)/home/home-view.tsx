"use client";

import { cn } from "@/lib/utils";
import { ArrowRight, Check, ChevronRight, MessageSquare, Plug, Plus, Search, Upload, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { LogoTile } from "@/components/brand";
import { PageHeader } from "@/components/page-header";
import { Kpi, Panel, PanelRow } from "@/components/panel";
import { SourceStatusPill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api/client";
import { useMe, useSources, type Source } from "@/lib/api/hooks";
import { absoluteTime, formatCount, relativeTime } from "@/lib/format";

/** Figures for the KPI strip, derived from the sources list (no extra endpoint). */
export function summarise(sources: Source[]) {
  const processing = sources.filter((s) => s.status === "queued" || s.status === "syncing").length;
  const failed = sources.filter((s) => s.status === "failed").length;
  const documents = sources.reduce((n, s) => n + s.documents, 0);
  const lastSynced = sources
    .map((s) => s.last_synced_at)
    .filter((t): t is string => !!t)
    .sort()
    .at(-1) ?? null;
  return { count: sources.length, documents, processing, failed, lastSynced };
}

/** Home (UI_DESIGN §4.8): what's indexed, what's processing, and what to do next. */
export function HomeView() {
  const me = useMe();
  const sources = useSources();
  const canAdd = !!me.data && !me.data.is_guest && me.data.role !== "viewer";
  const firstName = me.data?.user.name.split(/\s+/)[0];
  const s = sources.data ? summarise(sources.data) : null;
  const recent = [...(sources.data ?? [])]
    .sort((a, b) => (b.last_synced_at ?? "").localeCompare(a.last_synced_at ?? ""))
    .slice(0, 5);

  return (
    <>
      <PageHeader
        title="Home"
        description={me.data?.is_guest ? "Exploring the public demo" : firstName ? `Welcome back, ${firstName}` : undefined}
        actions={
          canAdd ? (
            <Button asChild variant="primary">
              <Link href="/sources?add=1">
                <Plus aria-hidden="true" />
                Add source
              </Link>
            </Button>
          ) : null
        }
      />

      {sources.isError ? (
        <p role="alert" className="mx-4 mt-6 rounded-md border border-danger-border bg-danger-subtle px-3 py-2.5 text-caption text-danger sm:mx-8">
          {errorMessage(sources.error)}{" "}
          <button className="font-medium underline" onClick={() => sources.refetch()}>
            Retry
          </button>
        </p>
      ) : (
        <>
          <PanelRow cols={4}>
            <Kpi label="Sources" value={s ? formatCount(s.count) : <Skeleton className="h-6 w-10" />} caption={s?.failed ? `${s.failed} need attention` : "Connected and searchable"} />
            <Kpi label="Documents" value={s ? formatCount(s.documents) : <Skeleton className="h-6 w-14" />} caption="Split, embedded and indexed" />
            <Kpi
              label="Processing"
              value={s ? formatCount(s.processing) : <Skeleton className="h-6 w-8" />}
              caption={s?.processing ? <LiveDot>Updating every few seconds</LiveDot> : "Nothing in the queue"}
            />
            <Kpi
              label="Last sync"
              value={s ? relativeTime(s.lastSynced) : <Skeleton className="h-6 w-20" />}
              title={absoluteTime(s?.lastSynced)}
              caption="Most recent successful run"
            />
          </PanelRow>

          <PanelRow cols={3} last>
            <Panel
              span={2}
              label="Recent sources"
              action={
                recent.length ? (
                  <Link href="/sources" className="inline-flex items-center gap-0.5 text-caption text-fg-muted hover:text-brand">
                    View all
                    <ChevronRight className="size-3.5" aria-hidden="true" />
                  </Link>
                ) : null
              }
            >
              {sources.isPending ? (
                <div className="flex flex-col gap-2" aria-label="Loading sources">
                  {[0, 1, 2].map((i) => (
                    <Skeleton key={i} className="h-11 w-full" />
                  ))}
                </div>
              ) : recent.length === 0 ? (
                <div className="flex flex-col items-start gap-3 rounded-md border border-dashed border-border-strong bg-subtle px-5 py-6">
                  <LogoTile connector="upload" size={32} />
                  <div>
                    <p className="text-ui font-semibold text-fg">{canAdd ? "Add your first source" : "Nothing shared with you yet"}</p>
                    <p className="mt-0.5 text-body text-fg-subtle">
                      {canAdd
                        ? "Upload a PDF, Word or Markdown file and Slate will index it in a few seconds."
                        : "Sources appear here once someone shares them with you."}
                    </p>
                  </div>
                  {canAdd ? (
                    <Button asChild variant="primary">
                      <Link href="/sources?add=1">
                        <Upload aria-hidden="true" />
                        Upload files
                      </Link>
                    </Button>
                  ) : null}
                </div>
              ) : (
                <ul className="-mx-2 flex flex-col">
                  {recent.map((src) => (
                    <li key={src.id}>
                      <Link href={`/sources/${src.id}`} className="flex items-center gap-3 rounded-md px-2 py-2 transition-colors hover:bg-subtle">
                        <LogoTile connector={src.connector} size={28} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-body font-medium text-fg">{src.name}</span>
                          <span className="block text-data text-fg-subtle tabular-nums">
                            {formatCount(src.documents)} {src.documents === 1 ? "document" : "documents"} · {relativeTime(src.last_synced_at)}
                          </span>
                        </span>
                        <SourceStatusPill status={src.status} />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>

            <Panel label="Get started" last>
              <ol className="flex flex-col gap-1">
                <Step done={!!s && s.count > 0} icon={Plug} title="Connect a source" body="Upload files now. GitHub, Discord and Gmail come next." href="/sources" />
                <Step icon={Search} title="Search everything" body="Hybrid keyword and meaning search, filtered to what you can see." href="/search" />
                <Step icon={MessageSquare} title="Ask with citations" body="Answers that link to their passages, arriving in M3." href="/chat" soon />
              </ol>
            </Panel>
          </PanelRow>
        </>
      )}
    </>
  );
}

function Step({
  done = false,
  soon = false,
  icon: Icon,
  title,
  body,
  href,
}: {
  done?: boolean;
  soon?: boolean;
  icon: LucideIcon;
  title: string;
  body: string;
  href: string;
}) {
  return (
    <li>
      <Link href={href} className="group -mx-2 flex items-start gap-3 rounded-md px-2 py-2.5 transition-colors hover:bg-subtle">
        <span
          className={cn(
            "mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-full border",
            done ? "border-success-border bg-success-subtle text-success" : "border-border bg-bg text-fg-muted",
          )}
        >
          {done ? <Check className="size-3.5" strokeWidth={2} aria-hidden="true" /> : <Icon className="size-3.5" strokeWidth={1.75} aria-hidden="true" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2 text-body font-medium text-fg">
            {title}
            {done ? <span className="sr-only">(done)</span> : null}
            {soon ? <span className="rounded-sm border border-brand-border px-1.5 text-label font-semibold tracking-wider text-brand uppercase">Soon</span> : null}
          </span>
          <span className="block text-caption text-fg-subtle">{body}</span>
        </span>
        <ArrowRight className="mt-1 size-3.5 shrink-0 text-fg-disabled transition-transform group-hover:translate-x-0.5 group-hover:text-fg-muted motion-reduce:transition-none" aria-hidden="true" />
      </Link>
    </li>
  );
}

function LiveDot({ children }: { children: string }) {
  return (
    <span className="inline-flex items-center gap-1.5" role="status">
      <span className="relative inline-flex size-1.5">
        <span className="absolute inset-0 animate-ping rounded-full bg-brand opacity-60 motion-reduce:hidden" />
        <span className="relative size-1.5 rounded-full bg-brand" />
      </span>
      {children}
    </span>
  );
}
