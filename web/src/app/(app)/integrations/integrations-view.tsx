"use client";

import { CircleAlert, Plus, Search, ShieldCheck, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { LogoTile } from "@/components/brand";
import { PageHeader } from "@/components/page-header";
import { Pill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { useIntegrations, useMe } from "@/lib/api/hooks";
import {
  accountSummary,
  brandKey,
  filterIntegrations,
  savePending,
  tabCounts,
  type Integration,
  type Tab,
} from "@/lib/integrations";
import { cn } from "@/lib/utils";
import { ManageDialog } from "./manage-dialog";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "connected", label: "Connected" },
  { id: "needs_sign_in", label: "Needs sign-in" },
];

/** Integrations (UI_DESIGN §4.9, ADR-0032): every provider, your state for each, one place to connect. */
export function IntegrationsView({ go = (url: string) => window.location.assign(url) }: { go?: (url: string) => void }) {
  const integrations = useIntegrations();
  const me = useMe();
  const [tab, setTab] = useState<Tab>("all");
  const [query, setQuery] = useState("");
  const [confirming, setConfirming] = useState<Integration | null>(null);
  // "Add a source" links here with ?connect=<id> (ADR-0033): ask to confirm that provider.
  const params = useSearchParams();
  const router = useRouter();
  const [deepDone, setDeepDone] = useState(false);
  const [managing, setManaging] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const canConnect = !!me.data && me.data.role !== "viewer";

  async function start(i: Integration) {
    setBusy(i.id);
    try {
      const { authorize_url } = unwrap(
        await api.POST("/api/v1/integrations/{provider_id}/authorize", { params: { path: { provider_id: i.id } } }),
      );
      savePending(i.id);
      go(authorize_url);
    } catch (err) {
      toast.error(errorMessage(err));
      setBusy(null);
    }
  }


  const list = integrations.data ?? [];
  const counts = tabCounts(list);
  const shown = filterIntegrations(list, tab, query);
  const deepId = deepDone ? null : params.get("connect");
  const deep = list.find(
    (i) => i.id === deepId && i.auth === "oauth2" && i.state !== "coming_soon" && i.state !== "not_set_up",
  );
  const asking = confirming ?? (canConnect ? deep : undefined) ?? null;
  const closeAsk = () => {
    setConfirming(null);
    if (deepId) {
      setDeepDone(true);
      router.replace("/integrations", { scroll: false });
    }
  };

  return (
    <>
      <PageHeader title="Integrations" description="Connect your accounts so Slate can search them. Only you can see what you connect." />
      <div className="mx-auto w-full max-w-[1120px] px-4 py-6 sm:px-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div role="tablist" aria-label="Filter integrations" className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                type="button"
                aria-selected={tab === t.id}
                onClick={() => setTab(t.id)}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-caption font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-brand",
                  tab === t.id ? "bg-fg text-bg" : "text-fg-muted hover:bg-muted hover:text-fg",
                )}
              >
                {t.label}
                {t.id !== "all" && counts[t.id] > 0 ? <span className="tabular-nums opacity-80">{counts[t.id]}</span> : null}
              </button>
            ))}
          </div>
          <label className="relative block w-full sm:w-72">
            <span className="sr-only">Search integrations</span>
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search integrations" className="h-9 pl-8" />
          </label>
        </div>

        {integrations.isPending ? (
          <ul className="grid gap-x-10 gap-y-2 md:grid-cols-2" aria-label="Loading integrations">
            {Array.from({ length: 8 }, (_, n) => (
              <li key={n} className="flex items-center gap-4 py-4">
                <Skeleton className="size-12 rounded-lg" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-4 w-32" />
                  <Skeleton className="h-3 w-56" />
                </div>
              </li>
            ))}
          </ul>
        ) : integrations.isError ? (
          <div role="alert" className="flex gap-3 rounded-lg border border-danger-border bg-danger-subtle px-4 py-4 text-caption">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
            <span>{errorMessage(integrations.error)}</span>
          </div>
        ) : shown.length === 0 ? (
          <p className="py-16 text-center text-caption text-fg-subtle">
            {tab === "needs_sign_in"
              ? "Nothing needs signing in again."
              : tab === "connected"
                ? "You haven't connected anything yet."
                : `No integrations match "${query.trim()}".`}
          </p>
        ) : (
          <ul className="grid gap-x-10 md:grid-cols-2">
            {shown.map((i) => (
              <Row
                key={i.id}
                integration={i}
                canConnect={canConnect}
                busy={busy === i.id}
                onConnect={() => (i.notice ? setConfirming(i) : void start(i))}
                onManage={() => setManaging(i.id)}
              />
            ))}
          </ul>
        )}
      </div>

      <Dialog open={asking !== null} onOpenChange={(open) => !open && closeAsk()}>
        <DialogContent className="sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle className="text-dialog font-semibold tracking-[-0.015em]">Connect {asking?.name}</DialogTitle>
            <DialogDescription className="text-body text-fg-muted">
              {asking?.notice ?? `You'll sign in to ${asking?.name ?? "the provider"} and choose what Slate may read. Slate only reads, never writes.`}
            </DialogDescription>
          </DialogHeader>
          <p className="flex items-center gap-2 text-caption text-fg-muted">
            <ShieldCheck className="size-4 text-success" aria-hidden="true" /> Read-only. Only you can search what you connect.
          </p>
          <DialogFooter>
            <Button variant="secondary" onClick={closeAsk}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={busy === asking?.id}
              onClick={() => asking && void start(asking)}
            >
              Continue to sign in
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ManageDialog
        integration={list.find((x) => x.id === managing) ?? null}
        onClose={() => setManaging(null)}
        onConnect={(i) => {
          setManaging(null);
          if (i.notice) setConfirming(i);
          else void start(i);
        }}
      />
    </>
  );
}

function Row({
  integration: i,
  canConnect,
  busy,
  onConnect,
  onManage,
}: {
  integration: Integration;
  canConnect: boolean;
  busy: boolean;
  onConnect: () => void;
  onManage: () => void;
}) {
  const account = accountSummary(i);
  return (
    <li className="flex items-center gap-4 border-b border-border-subtle py-4">
      <LogoTile connector={brandKey(i.id)} size={48} className="rounded-lg" />
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-1.5 truncate text-ui font-medium text-fg">
          {i.name}
          {(i.warnings ?? []).length > 0 ? (
            <TriangleAlert className="size-3.5 text-warning" aria-label="Needs your attention" />
          ) : null}
        </p>
        <p className="truncate text-caption text-fg-subtle" title={account ?? i.tagline}>
          {account ?? i.tagline}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <span className="hidden text-data text-fg-subtle sm:inline">{i.mode === "live" ? "Live lookup" : "Searchable"}</span>
        <Status state={i.state} />
        <Action integration={i} canConnect={canConnect} busy={busy} onConnect={onConnect} onManage={onManage} />
      </div>
    </li>
  );
}

function Status({ state }: { state: Integration["state"] }) {
  switch (state) {
    case "connected":
      return <Pill variant="success">Connected</Pill>;
    case "needs_reconnect":
      return <Pill variant="warning">Needs sign-in</Pill>;
    case "not_set_up":
      return <Pill variant="outline">Not set up</Pill>;
    case "coming_soon":
      return <Pill variant="outline">Coming soon</Pill>;
    default:
      return null;
  }
}

function Action({
  integration: i,
  canConnect,
  busy,
  onConnect,
  onManage,
}: {
  integration: Integration;
  canConnect: boolean;
  busy: boolean;
  onConnect: () => void;
  onManage: () => void;
}) {
  if (i.state === "coming_soon" || i.state === "not_set_up" || !canConnect) return <span className="w-24" aria-hidden="true" />;
  if (i.auth !== "oauth2") {
    const label = i.state === "connected" ? "Add more" : "Connect";
    return (
      <Button asChild variant="secondary" size="sm" className="w-24">
        <Link href={i.connect_path ?? "/sources?add=1"} aria-label={`${label} ${i.name}`}>
          {i.state === "connected" ? null : <Plus aria-hidden="true" />}
          {label}
        </Link>
      </Button>
    );
  }
  if (i.state === "needs_reconnect") {
    return (
      <Button variant="primary" size="sm" className="w-24" loading={busy} onClick={onConnect} aria-label={`Reconnect ${i.name}`}>
        Reconnect
      </Button>
    );
  }
  if (i.state === "connected") {
    return (
      <Button variant="secondary" size="sm" className="w-24" onClick={onManage} aria-label={`Manage ${i.name}`}>
        Manage
      </Button>
    );
  }
  return (
    <Button variant="secondary" size="sm" className="w-24" loading={busy} onClick={onConnect} aria-label={`Connect ${i.name}`}>
      <Plus aria-hidden="true" />
      Connect
    </Button>
  );
}
