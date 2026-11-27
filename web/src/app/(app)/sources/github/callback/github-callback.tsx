"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Clock, ExternalLink, Globe, Lock, Search } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import type { SyncReply } from "@/lib/sync-result";
import { LogoTile } from "@/components/brand";
import { PageHeader } from "@/components/page-header";
import { Pill, type PillVariant } from "@/components/status-pill";
import { Button, Spinner } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import { keys, type GitHubRepo } from "@/lib/api/hooks";
import {
  clearGrant,
  filterRepos,
  loadGrant,
  manageInstallationUrl,
  MAX_REPOS,
  parseCallback,
  saveGrant,
  toggleAll,
} from "@/lib/github";
import { cn } from "@/lib/utils";

type Phase =
  | { kind: "verifying" }
  | { kind: "approval" }
  | { kind: "error"; message: string; restart: boolean }
  | { kind: "picking"; grant: string; installationId: number }
  | { kind: "syncing"; rows: SyncRow[] };

type SyncRow = { sourceId: string; name: string; state: "waiting" | "syncing" | SyncReply["status"]; detail?: string };

const ROW_STATE: Record<SyncRow["state"], { variant: PillVariant; label: string }> = {
  waiting: { variant: "outline", label: "Waiting" },
  syncing: { variant: "accent", label: "Syncing" },
  done: { variant: "success", label: "Synced" },
  partial: { variant: "warning", label: "Partly synced" },
  background: { variant: "neutral", label: "Continues in background" },
  failed: { variant: "danger", label: "Couldn't sync" },
};

/**
 * GitHub's install redirect lands here (ADR-0023): verify the installation is this user's,
 * pick repositories, connect them, and run each first sync with live progress.
 */
export function GitHubCallback() {
  const params = useSearchParams();
  const router = useRouter();
  const client = useQueryClient();
  const [phase, setPhase] = useState<Phase>(() => initialPhase(parseCallback(params)));
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // the OAuth code works once: never send it twice
    started.current = true;
    const cb = parseCallback(params);
    if (cb.kind !== "install") return;
    // Drop the code and state from the address bar so a refresh or bookmark can't reuse them.
    router.replace("/sources/github/callback", { scroll: false });
    void (async () => {
      try {
        const { grant } = unwrap(
          await api.POST("/api/v1/sources/github/installations", {
            body: { installation_id: cb.installationId, code: cb.code, state: cb.state },
          }),
        );
        saveGrant({ grant, installationId: cb.installationId, at: Date.now() });
        setPhase({ kind: "picking", grant, installationId: cb.installationId });
      } catch (err) {
        setPhase({ kind: "error", message: errorMessage(err), restart: true });
      }
    })();
  }, [params, router]);

  async function connect(grant: string, names: string[]) {
    try {
      const connected = unwrap(await api.POST("/api/v1/sources/github/connect", { body: { grant, repos: names } }));
      clearGrant();
      void client.invalidateQueries({ queryKey: keys.sources });
      if (!connected.length) {
        toast.message("Those repositories were already connected.");
        router.push("/sources");
        return;
      }
      const rows: SyncRow[] = connected.map((c) => ({ sourceId: c.source_id, name: c.name, state: "waiting" }));
      setPhase({ kind: "syncing", rows });
      // One at a time: each first sync runs right away (up to ~90 s), then hands over to the worker.
      for (const row of rows) {
        setPhase((p) => (p.kind === "syncing" ? { ...p, rows: p.rows.map((r) => (r.sourceId === row.sourceId ? { ...r, state: "syncing" } : r)) } : p));
        let next: SyncRow;
        try {
          const res = unwrap(await api.POST("/api/v1/sources/{source_id}/sync", { params: { path: { source_id: row.sourceId } } }));
          // The run's real outcome (P0-2): a failure shows its reason, never "background".
          // Say what was left out too (P0-4), so "0 documents" is never a mystery.
          const skipped = res.skipped_summary ? `Not indexed: ${res.skipped_summary}.` : undefined;
          next = { ...row, state: res.status, detail: [res.reason, skipped].filter(Boolean).join(" ") || undefined };
        } catch (err) {
          next = { ...row, state: "failed", detail: errorMessage(err) };
        }
        setPhase((p) => (p.kind === "syncing" ? { ...p, rows: p.rows.map((r) => (r.sourceId === row.sourceId ? next : r)) } : p));
        void client.invalidateQueries({ queryKey: keys.sources });
      }
    } catch (err) {
      const expired = err instanceof ApiProblem && err.status === 422;
      if (expired) clearGrant();
      setPhase({ kind: "error", message: errorMessage(err), restart: expired });
    }
  }

  return (
    <>
      <PageHeader title="Connect GitHub" description="Choose which repositories Slate should search" />
      <div className="mx-auto w-full max-w-[720px] px-4 py-6 sm:px-8">
        {phase.kind === "verifying" ? (
          <p role="status" className="flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Checking your installation with GitHub…
          </p>
        ) : phase.kind === "approval" ? (
          <Notice icon={Clock} title="Waiting for approval">
            An owner of that GitHub organisation needs to approve Slate&apos;s app. Once they do, start again from Add source.
          </Notice>
        ) : phase.kind === "error" ? (
          <Notice icon={CircleAlert} title="Couldn't connect GitHub" tone="danger">
            {phase.message}
            {phase.restart ? (
              <span className="mt-3 block">
                <Button asChild variant="secondary" size="sm">
                  <Link href="/sources?add=1">Start again</Link>
                </Button>
              </span>
            ) : null}
          </Notice>
        ) : phase.kind === "picking" ? (
          <RepoPicker grant={phase.grant} installationId={phase.installationId} onConnect={(names) => connect(phase.grant, names)} onExpired={(m) => setPhase({ kind: "error", message: m, restart: true })} />
        ) : (
          <SyncProgress rows={phase.rows} />
        )}
      </div>
    </>
  );
}

/** Everything decidable without the network: approval wait, a refresh with a saved grant, or a bad link. */
function initialPhase(cb: ReturnType<typeof parseCallback>): Phase {
  if (cb.kind === "approval") return { kind: "approval" };
  if (cb.kind === "install") return { kind: "verifying" };
  const stored = loadGrant();
  return stored
    ? { kind: "picking", grant: stored.grant, installationId: stored.installationId }
    : { kind: "error", message: "GitHub didn't send back an installation, or this link was already used.", restart: true };
}

function RepoPicker({
  grant,
  installationId,
  onConnect,
  onExpired,
}: {
  grant: string;
  installationId: number;
  onConnect: (names: string[]) => Promise<void>;
  onExpired: (message: string) => void;
}) {
  const [repos, setRepos] = useState<GitHubRepo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const list = unwrap(await api.POST("/api/v1/sources/github/repos", { body: { grant } }));
        if (live) setRepos(list);
      } catch (err) {
        if (!live) return;
        if (err instanceof ApiProblem && err.status === 422) {
          clearGrant();
          onExpired(errorMessage(err));
        } else setLoadError(errorMessage(err));
      }
    })();
    return () => {
      live = false;
    };
  }, [grant, onExpired]);

  const visible = useMemo(() => filterRepos(repos ?? [], query), [repos, query]);
  const selectable = visible.filter((r) => !r.connected);
  const allOn = selectable.length > 0 && selectable.every((r) => selected.has(r.full_name));
  const anyPublic = (repos ?? []).some((r) => selected.has(r.full_name) && !r.private);

  if (loadError) {
    return (
      <Notice icon={CircleAlert} title="Couldn't list your repositories" tone="danger">
        {loadError}
      </Notice>
    );
  }
  if (!repos) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading repositories">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }
  if (!repos.length) {
    return (
      <Notice icon={CircleAlert} title="The app can't see any repositories">
        Give Slate&apos;s app access to at least one repository on GitHub, then come back.
        <span className="mt-3 block">
          <Button asChild variant="secondary" size="sm">
            <a href={manageInstallationUrl(installationId)} target="_blank" rel="noopener noreferrer">
              Choose repositories on GitHub <ExternalLink aria-hidden="true" />
            </a>
          </Button>
        </span>
      </Notice>
    );
  }

  const toggle = (name: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else if (next.size < MAX_REPOS) next.add(name);
      return next;
    });

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!selected.size) return;
        setBusy(true);
        void onConnect([...selected]).finally(() => setBusy(false));
      }}
    >
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border bg-bg px-3 focus-within:border-brand">
          <Search className="size-4 text-fg-subtle" aria-hidden="true" />
          <span className="sr-only">Filter repositories</span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter repositories" className="min-w-0 flex-1 bg-transparent text-body outline-none" />
        </label>
        <Button type="button" variant="ghost" size="sm" disabled={!selectable.length} onClick={() => setSelected((s) => toggleAll(s, visible))}>
          {allOn ? "Clear selection" : "Select all"}
        </Button>
      </div>

      <ul className="divide-y divide-border rounded-lg border border-border" aria-label="Repositories">
        {visible.map((r) => {
          const checked = selected.has(r.full_name);
          const atCap = !checked && selected.size >= MAX_REPOS;
          return (
            <li key={r.id}>
              <label className={cn("flex items-start gap-3 px-4 py-3", r.connected || atCap ? "cursor-not-allowed opacity-70" : "cursor-pointer hover:bg-subtle")}>
                <input
                  type="checkbox"
                  className="mt-1 size-4 accent-[var(--color-brand)]"
                  checked={r.connected || checked}
                  disabled={r.connected || atCap}
                  onChange={() => toggle(r.full_name)}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-body font-medium text-fg">{r.full_name}</span>
                    <Pill variant="outline" icon={r.private ? <Lock className="size-3" aria-hidden="true" /> : <Globe className="size-3" aria-hidden="true" />}>
                      {r.private ? "Private" : "Public"}
                    </Pill>
                    {r.connected ? <Pill variant="success">Connected</Pill> : null}
                  </span>
                  {r.description ? <span className="mt-0.5 block truncate text-caption text-fg-subtle">{r.description}</span> : null}
                </span>
              </label>
            </li>
          );
        })}
        {!visible.length ? <li className="px-4 py-6 text-center text-caption text-fg-subtle">No repositories match “{query}”.</li> : null}
      </ul>

      <p className="text-caption text-fg-subtle">
        Slate indexes each repository&apos;s docs (Markdown and text files, README and similar), issues and pull requests.
        It doesn&apos;t index code. After the first sync you&apos;ll see what was left out.
      </p>

      {anyPublic ? (
        <p className="rounded-md border border-border bg-subtle px-3 py-2 text-caption text-fg-muted">
          Public repositories are searchable only by you, just like private ones.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <a href={manageInstallationUrl(installationId)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-caption text-fg-subtle hover:text-fg">
          Missing a repository? Change access on GitHub <ExternalLink className="size-3" aria-hidden="true" />
        </a>
        <span className="flex items-center gap-3">
          <span className="text-caption text-fg-subtle tabular-nums" aria-live="polite">
            {selected.size} selected{selected.size >= MAX_REPOS ? ` (max ${MAX_REPOS})` : ""}
          </span>
          <Button type="submit" variant="primary" disabled={!selected.size} loading={busy}>
            Connect {selected.size || ""} {selected.size === 1 ? "repository" : "repositories"}
          </Button>
        </span>
      </div>
    </form>
  );
}

function SyncProgress({ rows }: { rows: SyncRow[] }) {
  const finished = rows.every((r) => r.state !== "waiting" && r.state !== "syncing");
  return (
    <div className="flex flex-col gap-4">
      <ul className="divide-y divide-border rounded-lg border border-border" aria-label="Sync progress">
        {rows.map((r) => {
          const st = ROW_STATE[r.state];
          return (
            <li key={r.sourceId} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <LogoTile connector="github" size={28} />
              <Link href={`/sources/${r.sourceId}`} className="min-w-0 flex-1 truncate text-body font-medium text-fg hover:text-brand">
                {r.name}
              </Link>
              <Pill variant={st.variant} icon={r.state === "syncing" ? <Spinner className="size-2.5" /> : undefined}>
                {st.label}
              </Pill>
              {r.detail ? (
                <p className={`w-full text-caption ${r.state === "failed" ? "text-danger" : "text-fg-muted"}`}>{r.detail}</p>
              ) : null}
            </li>
          );
        })}
      </ul>
      <p className="text-caption text-fg-muted" aria-live="polite">
        {finished
          ? "Done. Large repositories keep syncing in the background, and every repository re-syncs hourly."
          : "Indexing docs, issues and pull requests. You can leave this page; syncing continues."}
      </p>
      <div>
        <Button asChild variant={finished ? "primary" : "secondary"}>
          <Link href="/sources">Go to Sources</Link>
        </Button>
      </div>
    </div>
  );
}

function Notice({
  icon: Icon,
  title,
  tone = "neutral",
  children,
}: {
  icon: typeof Clock;
  title: string;
  tone?: "neutral" | "danger";
  children: React.ReactNode;
}) {
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex gap-3 rounded-lg border px-4 py-4", tone === "danger" ? "border-danger-border bg-danger-subtle" : "border-border bg-subtle")}>
      <Icon className={cn("mt-0.5 size-4 shrink-0", tone === "danger" ? "text-danger" : "text-fg-muted")} aria-hidden="true" />
      <div className="text-caption text-fg-body">
        <p className="mb-1 text-ui font-semibold text-fg">{title}</p>
        {children}
      </div>
    </div>
  );
}
