"use client";

import { cn } from "@/lib/utils";
import { SearchX } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Kpi, Panel, PanelRow } from "@/components/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiProblem, errorMessage } from "@/lib/api/client";
import { useAdminOverview, useAdminSignups, useMe, type AdminOverview, type AdminRange } from "@/lib/api/hooks";
import { formatCount } from "@/lib/format";
import { useState } from "react";
import { BarChart } from "./bar-chart";
import { percent, weekDelta } from "./labels";
import { SecurityPanel } from "./security-panel";
import { UsersPanel } from "./users-panel";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "security", label: "Security" },
] as const;
type Tab = (typeof TABS)[number]["id"];

const RANGES: { days: AdminRange; label: string }[] = [
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 365, label: "12 months" },
];

/** The tab button style shared with Integrations (one look for every segmented control). */
export function segment(selected: boolean) {
  return cn(
    "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-caption font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-brand",
    selected ? "bg-fg text-bg" : "text-fg-muted hover:bg-muted hover:text-fg",
  );
}

/**
 * Admin (ADR-0042): platform-wide account health for the verified OWNER_EMAIL account.
 * Account metadata only: no one's documents, chats or connections appear here. Anyone else
 * sees the same "not found" page as any unknown address (the API answers 404 too).
 */
export function AdminView() {
  const me = useMe();
  const overview = useAdminOverview();
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const raw = params.get("tab");
  const tab: Tab = TABS.some((t) => t.id === raw) ? (raw as Tab) : "overview";

  const hidden =
    (me.data !== undefined && !me.data?.is_platform_admin) ||
    (overview.error instanceof ApiProblem && overview.error.status === 404);
  if (hidden) return <NotFound />;

  const select = (next: Tab) => {
    const q = new URLSearchParams(params);
    if (next === "overview") q.delete("tab");
    else q.set("tab", next);
    router.replace(`${pathname}${q.size ? `?${q}` : ""}`, { scroll: false });
  };

  return (
    <>
      <PageHeader title="Admin" description="Accounts across Slate. Nobody's documents or chats are shown here." />
      <div className="border-b border-border px-4 py-2 sm:px-8">
        <div role="tablist" aria-label="Admin sections" className="flex gap-1">
          {TABS.map((t) => (
            <button key={t.id} id={`tab-${t.id}`} role="tab" type="button" aria-selected={tab === t.id} aria-controls={`panel-${t.id}`} onClick={() => select(t.id)} className={segment(tab === t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div id={`panel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" ? <OverviewPanel overview={overview} /> : tab === "users" ? <UsersPanel /> : <SecurityPanel />}
      </div>
    </>
  );
}

function NotFound() {
  return (
    <EmptyState icon={SearchX} title="Page not found">
      There&apos;s nothing at this address. Check the link, or go back to Home.
    </EmptyState>
  );
}

function OverviewPanel({ overview }: { overview: ReturnType<typeof useAdminOverview> }) {
  const [days, setDays] = useState<AdminRange>(30);
  const signups = useAdminSignups(days);
  const o = overview.data;

  if (overview.isError) {
    return (
      <p role="alert" className="mx-4 mt-6 rounded-md border border-danger-border bg-danger-subtle px-3 py-2.5 text-caption text-danger sm:mx-8">
        {errorMessage(overview.error)}{" "}
        <button className="font-medium underline" onClick={() => overview.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  const tile = (w: string) => <Skeleton className={cn("h-6", w)} />;
  return (
    <>
      <PanelRow cols={4}>
        <Kpi label="Accounts" value={o ? formatCount(o.total) : tile("w-12")} caption={o ? `${formatCount(o.new_today)} new today` : " "} />
        <Kpi label="New this week" value={o ? formatCount(o.new_7d) : tile("w-10")} caption={o ? weekDelta(o.new_7d, o.new_prev_7d) : " "} />
        <Kpi
          label="Verified"
          value={o ? percent(o.verified, o.total) : tile("w-12")}
          caption={o ? `${formatCount(o.verified)} of ${formatCount(o.total)} accounts` : " "}
        />
        <Kpi
          label="Active, 7 days"
          value={o ? formatCount(o.active_7d) : tile("w-10")}
          caption={o ? `${formatCount(o.active_30d)} in the last 30 days` : " "}
          title="Signed in or used Slate. Measured to within about 5 minutes."
        />
      </PanelRow>
      <PanelRow cols={3} last>
        <Panel
          label="Sign-ups"
          span={2}
          action={
            <div role="radiogroup" aria-label="Time range" className="flex gap-1">
              {RANGES.map((r) => (
                <button key={r.days} type="button" role="radio" aria-checked={days === r.days} onClick={() => setDays(r.days)} className={cn(segment(days === r.days), "h-7 px-2.5")}>
                  {r.label}
                </button>
              ))}
            </div>
          }
        >
          <BarChart points={signups.data?.points} bucket={signups.data?.bucket ?? "day"} unit={["sign-up", "sign-ups"]} loading={signups.isFetching && signups.isPlaceholderData} />
          {signups.isError ? (
            <p role="alert" className="mt-2 text-caption text-danger">
              {errorMessage(signups.error)}
            </p>
          ) : null}
        </Panel>
        <Panel label="Account status" last>
          {o ? <StatusBreakdown o={o} /> : <Skeleton className="h-28 w-full" />}
        </Panel>
      </PanelRow>
    </>
  );
}

/** Verified / unverified / disabled (they partition the total) as one stacked bar with a
 * labelled legend, so status is never colour alone. */
function StatusBreakdown({ o }: { o: AdminOverview }) {
  const rows = [
    { key: "verified", label: "Verified", value: o.verified, swatch: "bg-success" },
    { key: "unverified", label: "Unverified", value: o.unverified, swatch: "bg-warning" },
    { key: "disabled", label: "Disabled", value: o.disabled, swatch: "bg-danger" },
  ];
  return (
    <div>
      <div className="flex h-2.5 w-full gap-[2px] overflow-hidden rounded-full bg-muted" aria-hidden="true">
        {rows
          .filter((r) => r.value > 0)
          .map((r) => (
            <div key={r.key} className={r.swatch} style={{ width: `${(r.value / Math.max(1, o.total)) * 100}%` }} />
          ))}
      </div>
      <ul className="mt-4 space-y-2.5">
        {rows.map((r) => (
          <li key={r.key} className="flex items-center gap-2 text-caption">
            <span className={cn("size-2 shrink-0 rounded-full", r.swatch)} aria-hidden="true" />
            <span className="flex-1 text-fg-body">{r.label}</span>
            <span className="font-medium text-fg tabular-nums">{formatCount(r.value)}</span>
            <span className="w-10 text-right text-fg-subtle tabular-nums">{percent(r.value, o.total)}</span>
          </li>
        ))}
      </ul>
      <p className="mt-4 text-data text-fg-subtle">
        Unverified accounts are deleted after 48 hours. {formatCount(o.failed_logins_24h)} failed sign-in{o.failed_logins_24h === 1 ? "" : "s"} in the last 24 hours.
      </p>
    </div>
  );
}
