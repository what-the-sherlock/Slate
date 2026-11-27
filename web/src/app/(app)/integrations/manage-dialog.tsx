"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Plus, RefreshCw, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { LogoTile } from "@/components/brand";
import { Pill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys, useSources, type Source } from "@/lib/api/hooks";
import { itemNoun } from "@/lib/connectors";
import { brandKey, type Integration } from "@/lib/integrations";

type Connection = Integration["connections"][number];

/**
 * Manage a provider's connected accounts (UI_DESIGN §4.9, ADR-0033): each account with its
 * sync state, Sync now, Open source, Reconnect when needed, Disconnect; and Add another.
 */
export function ManageDialog({
  integration,
  onClose,
  onConnect,
}: {
  integration: Integration | null;
  onClose: () => void;
  onConnect: (i: Integration) => void;
}) {
  const sources = useSources(integration !== null);
  const [confirming, setConfirming] = useState<Connection | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const client = useQueryClient();
  const byId = new Map((sources.data ?? []).map((s) => [s.id, s]));
  const i = integration;

  async function syncNow(c: Connection) {
    setBusy(`sync:${c.id}`);
    try {
      const res = unwrap(
        await api.POST("/api/v1/integrations/connections/{connection_id}/sync", {
          params: { path: { connection_id: c.id } },
        }),
      );
      toast.success(res.status === "done" ? "Synced" : "Synced what fit; the rest continues in the background.");
      void client.invalidateQueries({ queryKey: keys.integrations });
      void client.invalidateQueries({ queryKey: keys.sources });
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(c: Connection) {
    setBusy(`disconnect:${c.id}`);
    try {
      const r = await api.DELETE("/api/v1/integrations/connections/{connection_id}", {
        params: { path: { connection_id: c.id } },
      });
      if (!r.response.ok) unwrap(r);
      toast.success(`${c.account_label ?? i?.name ?? "Account"} disconnected`);
      void client.invalidateQueries({ queryKey: keys.integrations });
      void client.invalidateQueries({ queryKey: keys.sources });
      setConfirming(null);
      if (i && i.connections.length <= 1) onClose();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const confirmSource = confirming?.source_id ? byId.get(confirming.source_id) : undefined;

  return (
    <>
      <Dialog open={i !== null && confirming === null} onOpenChange={(open) => !open && onClose()}>
        <DialogContent className="sm:max-w-[560px]">
          {i ? (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-3 text-dialog font-semibold tracking-[-0.015em]">
                  <LogoTile connector={brandKey(i.id)} size={28} /> {i.name}
                </DialogTitle>
                <DialogDescription className="text-body text-fg-muted">
                  Only you can search what you connect. Slate reads, never writes.
                </DialogDescription>
              </DialogHeader>
              {(i.warnings ?? []).map((w) => (
                <p key={w} role="note" className="flex gap-2 rounded-md border border-warning-border bg-warning-subtle px-3 py-2 text-caption text-fg-body">
                  <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                  {w}
                </p>
              ))}
              <ul className="divide-y divide-border-subtle rounded-lg border border-border" aria-label={`${i.name} accounts`}>
                {i.connections.map((c) => (
                  <AccountRow
                    key={c.id}
                    connection={c}
                    connector={i.id}
                    source={c.source_id ? byId.get(c.source_id) : undefined}
                    busy={busy}
                    onSync={() => void syncNow(c)}
                    onReconnect={() => onConnect(i)}
                    onDisconnect={() => setConfirming(c)}
                  />
                ))}
              </ul>
              <DialogFooter>
                <Button variant="secondary" onClick={() => onConnect(i)}>
                  <Plus aria-hidden="true" /> Add another account
                </Button>
                <Button variant="primary" onClick={onClose}>
                  Done
                </Button>
              </DialogFooter>
            </>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <DialogContent className="sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle className="text-dialog font-semibold tracking-[-0.015em]">
              Disconnect {confirming?.account_label ?? i?.name}?
            </DialogTitle>
            <DialogDescription className="text-body text-fg-muted">
              Slate revokes its access at {i?.name} and deletes{" "}
              {confirmSource ? `${itemNoun(i?.id ?? "", confirmSource.documents)} indexed from this account` : "everything indexed from this account"}
              . Answers stop citing them right away. You can connect again later.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={busy === `disconnect:${confirming?.id}`}
              onClick={() => confirming && void disconnect(confirming)}
            >
              Disconnect
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function AccountRow({
  connection: c,
  connector,
  source,
  busy,
  onSync,
  onReconnect,
  onDisconnect,
}: {
  connection: Connection;
  connector: string;
  source: Source | undefined;
  busy: string | null;
  onSync: () => void;
  onReconnect: () => void;
  onDisconnect: () => void;
}) {
  const needsSignIn = c.status === "needs_reconnect";
  const syncing = source?.status === "syncing" || source?.status === "queued";
  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="truncate text-ui font-medium text-fg" title={c.account_label ?? undefined}>
          {c.account_label ?? "Connected account"}
        </p>
        <p className="truncate text-caption text-fg-subtle">{accountLine(connector, source, needsSignIn)}</p>
      </div>
      {needsSignIn ? (
        <Pill variant="warning">Needs sign-in</Pill>
      ) : syncing ? (
        <Pill variant="accent">Syncing</Pill>
      ) : source?.status === "failed" ? (
        <Pill variant="danger">Last sync failed</Pill>
      ) : (
        <Pill variant="success">Connected</Pill>
      )}
      <div className="flex items-center gap-1">
        {needsSignIn ? (
          <Button variant="primary" size="sm" onClick={onReconnect}>
            Reconnect
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Sync ${c.account_label ?? "account"} now`}
            title="Sync now"
            loading={busy === `sync:${c.id}`}
            disabled={syncing}
            onClick={onSync}
          >
            <RefreshCw aria-hidden="true" />
          </Button>
        )}
        {c.source_id ? (
          <Button asChild variant="ghost" size="icon-sm" title="Open source">
            <Link href={`/sources/${c.source_id}`} aria-label={`Open the ${c.account_label ?? "account"} source`}>
              <ExternalLink aria-hidden="true" />
            </Link>
          </Button>
        ) : null}
        <Button variant="ghost" size="sm" onClick={onDisconnect} aria-label={`Disconnect ${c.account_label ?? "account"}`}>
          Disconnect
        </Button>
      </div>
    </li>
  );
}

/** One line under an account: what's indexed and when, or what the user must do. */
export function accountLine(connector: string, source: Source | undefined, needsSignIn: boolean): string {
  if (needsSignIn) return "Access was revoked or expired. Reconnect to keep it up to date; what's indexed stays searchable.";
  if (!source) return "Setting up. Use sync now if nothing appears in a few minutes.";
  const count = itemNoun(connector, source.documents);
  if (source.status === "never") return "First sync queued";
  if (source.last_synced_at) return `${count} · synced ${relative(source.last_synced_at)}`;
  return count;
}

function relative(iso: string, now = Date.now()): string {
  const mins = Math.round((now - Date.parse(iso)) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
