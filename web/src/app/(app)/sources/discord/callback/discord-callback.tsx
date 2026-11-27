"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Hash, Lock, MessagesSquare, Megaphone, Search } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { LogoTile } from "@/components/brand";
import { PageHeader } from "@/components/page-header";
import { Pill, type PillVariant } from "@/components/status-pill";
import { Button, Spinner } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import type { SyncReply } from "@/lib/sync-result";
import { keys, type DiscordChannel, type DiscordServer } from "@/lib/api/hooks";
import { clearGrant, groupChannels, loadGrant, MAX_CHANNELS, parseCallback, pickable, saveGrant, toggleAll } from "@/lib/discord";
import { cn } from "@/lib/utils";

type SyncState = "syncing" | SyncReply["status"];
type Phase =
  | { kind: "verifying" }
  | { kind: "error"; title: string; message: string; restart: boolean }
  | { kind: "picking"; grant: string }
  | { kind: "syncing"; sourceId: string; name: string; channels: number; state: SyncState; detail?: string };

const SYNC_PILL: Record<SyncState, { variant: PillVariant; label: string }> = {
  syncing: { variant: "accent", label: "Syncing" },
  done: { variant: "success", label: "Synced" },
  partial: { variant: "warning", label: "Partly synced" },
  background: { variant: "neutral", label: "Continues in background" },
  failed: { variant: "danger", label: "Couldn't sync" },
};

const KIND_ICON = { text: Hash, announcement: Megaphone, forum: MessagesSquare, media: MessagesSquare } as const;

/**
 * Discord's "add bot" redirect lands here (ADR-0027): verify the bot was added by this user,
 * pick channels they can read, connect them as one source, and run the first sync.
 */
export function DiscordCallback() {
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
    router.replace("/sources/discord/callback", { scroll: false });
    void (async () => {
      try {
        const { grant } = unwrap(
          await api.POST("/api/v1/sources/discord/installations", {
            body: { guild_id: cb.guildId, code: cb.code, state: cb.state },
          }),
        );
        saveGrant({ grant, at: Date.now() });
        setPhase({ kind: "picking", grant });
      } catch (err) {
        setPhase({ kind: "error", title: "Couldn't connect Discord", message: errorMessage(err), restart: true });
      }
    })();
  }, [params, router]);

  async function connect(grant: string, ids: string[]) {
    try {
      const done = unwrap(await api.POST("/api/v1/sources/discord/connect", { body: { grant, channels: ids } }));
      clearGrant();
      void client.invalidateQueries({ queryKey: keys.sources });
      setPhase({ kind: "syncing", sourceId: done.source_id, name: done.name, channels: done.channels, state: "syncing" });
      let state: SyncState = "background";
      let detail: string | undefined;
      try {
        // The first sync runs right away (up to ~90 s), then hands over to the worker.
        const res = unwrap(await api.POST("/api/v1/sources/{source_id}/sync", { params: { path: { source_id: done.source_id } } }));
        state = res.status; // the run's real outcome (P0-2)
        detail = res.reason ?? undefined;
      } catch (err) {
        state = "failed";
        detail = errorMessage(err);
      }
      setPhase((p) => (p.kind === "syncing" ? { ...p, state, detail } : p));
      void client.invalidateQueries({ queryKey: keys.sources });
    } catch (err) {
      const expired = err instanceof ApiProblem && err.status === 422;
      if (expired) clearGrant();
      setPhase({ kind: "error", title: "Couldn't connect Discord", message: errorMessage(err), restart: expired });
    }
  }

  return (
    <>
      <PageHeader title="Connect Discord" description="Choose which channels Slate should search" />
      <div className="mx-auto w-full max-w-[720px] px-4 py-6 sm:px-8">
        {phase.kind === "verifying" ? (
          <p role="status" className="flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Checking with Discord that you added Slate&apos;s bot…
          </p>
        ) : phase.kind === "error" ? (
          <Notice title={phase.title}>
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
          <ChannelPicker
            grant={phase.grant}
            onConnect={(ids) => connect(phase.grant, ids)}
            onExpired={(m) => setPhase({ kind: "error", title: "This link has expired", message: m, restart: true })}
          />
        ) : (
          <SyncProgress phase={phase} />
        )}
      </div>
    </>
  );
}

/** Everything decidable without the network: a cancel, a refresh with a saved grant, or a bad link. */
function initialPhase(cb: ReturnType<typeof parseCallback>): Phase {
  if (cb.kind === "denied") {
    return { kind: "error", title: "Discord wasn't connected", message: "You cancelled on Discord's page. Nothing was added.", restart: true };
  }
  if (cb.kind === "install") return { kind: "verifying" };
  const stored = loadGrant();
  return stored
    ? { kind: "picking", grant: stored.grant }
    : {
        kind: "error",
        title: "Couldn't connect Discord",
        message: "Discord didn't send back a server, or this link was already used.",
        restart: true,
      };
}

function ChannelPicker({
  grant,
  onConnect,
  onExpired,
}: {
  grant: string;
  onConnect: (ids: string[]) => Promise<void>;
  onExpired: (message: string) => void;
}) {
  const [server, setServer] = useState<DiscordServer | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const s = unwrap(await api.POST("/api/v1/sources/discord/channels", { body: { grant } }));
        if (live) setServer(s);
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

  const channels = useMemo(() => server?.channels ?? [], [server]);
  const groups = useMemo(() => groupChannels(channels, query), [channels, query]);
  const visible = useMemo(() => groups.flatMap((g) => g.channels), [groups]);
  const connectedCount = channels.filter((c) => c.connected).length;
  const room = MAX_CHANNELS - connectedCount;
  const visiblePickable = visible.filter(pickable);
  const allOn = visiblePickable.length > 0 && visiblePickable.every((c) => selected.has(c.id));
  const anyPrivate = channels.some((c) => selected.has(c.id) && c.private);

  if (loadError) return <Notice title="Couldn't list the server's channels">{loadError}</Notice>;
  if (!server) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading channels">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-11 w-full" />
        ))}
      </div>
    );
  }
  if (!channels.some(pickable) && !connectedCount) {
    return (
      <Notice title="No channels to connect">
        Slate&apos;s bot and you need to be able to read a channel to connect it. Give the bot&apos;s role access to the
        channels you want in Discord&apos;s channel settings, then start again.
      </Notice>
    );
  }

  const toggle = (id: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else if (next.size < room) next.add(id);
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
      <div className="flex items-center gap-3">
        <LogoTile connector="discord" size={32} />
        <div className="min-w-0">
          <p className="truncate text-ui font-semibold text-fg">{server.name}</p>
          <p className="text-caption text-fg-subtle">
            {channels.length} channels{connectedCount ? ` · ${connectedCount} connected` : ""}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border bg-bg px-3 focus-within:border-brand">
          <Search className="size-4 text-fg-subtle" aria-hidden="true" />
          <span className="sr-only">Filter channels</span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter channels" className="min-w-0 flex-1 bg-transparent text-body outline-none" />
        </label>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!visiblePickable.length}
          onClick={() => setSelected((s) => toggleAll(s, visible, connectedCount))}
        >
          {allOn ? "Clear selection" : "Select all"}
        </Button>
      </div>

      <div className="flex flex-col gap-3">
        {groups.map((g) => (
          <section key={g.category ?? "__none"} aria-label={g.category ?? "No category"}>
            <h3 className="mb-1 px-1 text-data font-semibold uppercase tracking-wide text-fg-subtle">{g.category ?? "No category"}</h3>
            <ul className="divide-y divide-border rounded-lg border border-border">
              {g.channels.map((c) => (
                <ChannelRow key={c.id} channel={c} checked={selected.has(c.id)} atCap={!selected.has(c.id) && selected.size >= room} onToggle={() => toggle(c.id)} />
              ))}
            </ul>
          </section>
        ))}
        {!groups.length ? <p className="px-4 py-6 text-center text-caption text-fg-subtle">No channels match “{query}”.</p> : null}
      </div>

      {anyPrivate ? (
        <p className="rounded-md border border-border bg-subtle px-3 py-2 text-caption text-fg-muted">
          Private channels stay private: only you can search them in Slate.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center justify-end gap-3">
        <span className="text-caption text-fg-subtle tabular-nums" aria-live="polite">
          {selected.size} selected{selected.size >= room ? ` (max ${MAX_CHANNELS} per server)` : ""}
        </span>
        <Button type="submit" variant="primary" disabled={!selected.size} loading={busy}>
          Connect {selected.size || ""} {selected.size === 1 ? "channel" : "channels"}
        </Button>
      </div>
    </form>
  );
}

function ChannelRow({ channel: c, checked, atCap, onToggle }: { channel: DiscordChannel; checked: boolean; atCap: boolean; onToggle: () => void }) {
  const Icon = KIND_ICON[c.kind as keyof typeof KIND_ICON] ?? Hash;
  const disabled = !pickable(c) || atCap;
  return (
    <li>
      <label className={cn("flex items-center gap-3 px-4 py-2.5", disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-subtle", !c.selectable && "opacity-60")}>
        <input type="checkbox" className="size-4 accent-[var(--color-brand)]" checked={c.connected || checked} disabled={disabled} onChange={onToggle} />
        <Icon className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="truncate text-body font-medium text-fg">{c.name}</span>
            {c.private ? (
              <Pill variant="outline" icon={<Lock className="size-3" aria-hidden="true" />}>
                Private
              </Pill>
            ) : null}
            {c.kind === "forum" || c.kind === "media" ? <Pill variant="outline">Forum</Pill> : null}
            {c.connected ? <Pill variant="success">Connected</Pill> : null}
          </span>
          {c.reason ? <span className="mt-0.5 block text-caption text-fg-subtle">{c.reason}</span> : null}
        </span>
      </label>
    </li>
  );
}

function SyncProgress({ phase }: { phase: Extract<Phase, { kind: "syncing" }> }) {
  const st = SYNC_PILL[phase.state];
  const finished = phase.state !== "syncing";
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-4 py-3">
        <LogoTile connector="discord" size={28} />
        <Link href={`/sources/${phase.sourceId}`} className="min-w-0 flex-1 truncate text-body font-medium text-fg hover:text-brand">
          {phase.name} · {phase.channels} {phase.channels === 1 ? "channel" : "channels"}
        </Link>
        <Pill variant={st.variant} icon={phase.state === "syncing" ? <Spinner className="size-2.5" /> : undefined}>
          {st.label}
        </Pill>
        {phase.detail ? (
          <p className={`w-full text-caption ${phase.state === "failed" ? "text-danger" : "text-fg-muted"}`}>{phase.detail}</p>
        ) : null}
      </div>
      <p className="text-caption text-fg-muted" aria-live="polite">
        {finished
          ? "Done. Busy servers keep syncing in the background, and new messages arrive with the hourly sync."
          : "Reading the last 90 days of messages and threads. You can leave this page; syncing continues."}
      </p>
      <div>
        <Button asChild variant={finished ? "primary" : "secondary"}>
          <Link href="/sources">Go to Sources</Link>
        </Button>
      </div>
    </div>
  );
}

function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div role="alert" className="flex gap-3 rounded-lg border border-danger-border bg-danger-subtle px-4 py-4">
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
      <div className="text-caption text-fg-body">
        <p className="mb-1 text-ui font-semibold text-fg">{title}</p>
        {children}
      </div>
    </div>
  );
}
