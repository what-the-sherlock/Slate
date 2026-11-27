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

type Phase = { kind: "working" } | { kind: "error"; title: string; message: string };

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
        void client.invalidateQueries({ queryKey: keys.integrations });
        toast.success(conn.account_label ? `Connected ${conn.account_label}` : "Connected");
        router.replace("/integrations");
      } catch (err) {
        setPhase({ kind: "error", title: "Couldn't finish connecting", message: errorMessage(err) });
      }
    })();
  }, [callback, router, client]);

  return (
    <>
      <PageHeader title="Connecting" />
      <div className="mx-auto w-full max-w-[720px] px-4 py-6 sm:px-8">
        {phase.kind === "working" ? (
          <p role="status" className="flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Finishing the sign-in…
          </p>
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
