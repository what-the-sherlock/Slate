"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { PageHeader } from "@/components/page-header";
import { Button, Spinner } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/hooks";
import { clearPending, parseCallback, peekPending, type Callback } from "@/lib/integrations";
import { syncMessage, type SyncReply } from "@/lib/sync-result";

type Phase =
  | { kind: "working" }
  | { kind: "syncing"; who: string }
  | { kind: "error"; title: string; message: string }
  | { kind: "sync_failed"; who: string; reason: string; connectionId: string; sourceId: string | null };

function initialPhase(cb: Callback): Phase {
  if (cb.kind === "denied") {
    return { kind: "error", title: "Nothing was connected", message: "You cancelled the sign-in, so Slate has no access." };
  }
  if (cb.kind === "missing") {
    return { kind: "error", title: "This sign-in link has expired", message: "Start connecting again from Integrations." };
  }
  return { kind: "working" };
}

/**
 * Every OAuth provider redirects here (ADR-0032). The code and state are removed from the
 * address bar at once, sent to the API in a POST body exactly once, and the user returns to
 * Integrations.
 */
export function IntegrationsCallback() {
  const params = useSearchParams();
  const router = useRouter();
  const client = useQueryClient();
  // Decided from the URL before the first render: a cancel or a stray redirect needs no
  // effect at all (and no state update inside one).
  const [callback] = useState(() => parseCallback(params, peekPending()));
  const [phase, setPhase] = useState<Phase>(() => initialPhase(callback));
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // the code works once: never send it twice (StrictMode)
    started.current = true;
    clearPending();
    router.replace("/integrations/callback", { scroll: false });
    const cb = callback;
    if (cb.kind !== "code") return;
    void (async () => {
      try {
        const conn = unwrap(
          await api.POST("/api/v1/integrations/{provider_id}/callback", {
            params: { path: { provider_id: cb.provider } },
            body: { code: cb.code, state: cb.state },
          }),
        );
        const who = conn.account_label ?? "your account";
        // The first sync runs now (up to ~90 s), so the account isn't empty for an hour.
        await firstSync(conn.id, who);
      } catch (err) {
        setPhase({ kind: "error", title: "Couldn't finish connecting", message: errorMessage(err) });
      }
    })();
  }, [callback, router, client]); // eslint-disable-line react-hooks/exhaustive-deps -- runs once

  /** The first sync runs now (up to ~90 s), so the account isn't empty for an hour. Its
   * real outcome is shown: a failure stays on screen with its reason (P0-2). */
  async function firstSync(connectionId: string, who: string) {
    setPhase({ kind: "syncing", who });
    let reply: SyncReply;
    let sourceId: string | null = null;
    try {
      const res = unwrap(
        await api.POST("/api/v1/integrations/connections/{connection_id}/sync", {
          params: { path: { connection_id: connectionId } },
        }),
      );
      reply = res;
      sourceId = res.source_id;
    } catch (err) {
      reply = { status: "failed", reason: errorMessage(err) };
    }
    void client.invalidateQueries({ queryKey: keys.integrations });
    void client.invalidateQueries({ queryKey: keys.sources });
    if (reply.status === "failed") {
      const reason = reply.reason ?? "Something went wrong. Try again.";
      setPhase({ kind: "sync_failed", who, reason, connectionId, sourceId });
      return;
    }
    if (reply.status === "done") toast.success(`Connected and synced ${who}.`);
    else toast.info(`Connected ${who}. ${syncMessage(reply, who).text}`);
    router.replace("/integrations");
  }

  return (
    <>
      <PageHeader title="Connecting" />
      <div className="mx-auto w-full max-w-[720px] px-4 py-6 sm:px-8">
        {phase.kind === "working" ? (
          <p role="status" className="flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Finishing the sign-in…
          </p>
        ) : phase.kind === "syncing" ? (
          <p role="status" className="flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Connected {phase.who}. Syncing your first items, which takes
            up to a minute and a half…
          </p>
        ) : phase.kind === "sync_failed" ? (
          <div role="alert" className="flex gap-3 rounded-lg border border-danger-border bg-danger-subtle px-4 py-4">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
            <div className="text-caption text-fg-body">
              <p className="mb-1 text-ui font-semibold text-fg">Connected {phase.who}, but the first sync failed</p>
              {phase.reason}
              <span className="mt-3 flex flex-wrap gap-2">
                <Button size="sm" onClick={() => void firstSync(phase.connectionId, phase.who)}>
                  Try again
                </Button>
                {phase.sourceId ? (
                  <Button asChild variant="secondary" size="sm">
                    <Link href={`/sources/${phase.sourceId}`}>Open source</Link>
                  </Button>
                ) : null}
                <Button asChild variant="ghost" size="sm">
                  <Link href="/integrations">Back to Integrations</Link>
                </Button>
              </span>
            </div>
          </div>
        ) : (
          <div role="alert" className="flex gap-3 rounded-lg border border-danger-border bg-danger-subtle px-4 py-4">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
            <div className="text-caption text-fg-body">
              <p className="mb-1 text-ui font-semibold text-fg">{phase.title}</p>
              {phase.message}
              <span className="mt-3 block">
                <Button asChild variant="secondary" size="sm">
                  <Link href="/integrations">Back to Integrations</Link>
                </Button>
              </span>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
