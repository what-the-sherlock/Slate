"use client";

import { CircleAlert, Users } from "lucide-react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";
import { Logo } from "@/components/brand";
import { Button, Spinner } from "@/components/ui/button";
import { resetWorkspaceState } from "@/lib/workspace-reset";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { useMe } from "@/lib/api/hooks";
import { readInviteToken, clearInviteToken } from "@/lib/invite";

const noSubscribe = () => () => {};

/**
 * Where an invitation link lands (ADR-0030). The token is in the URL fragment, which the
 * browser never sends to a server; it's kept for this tab only, so signing in or up first
 * doesn't lose it. Accepting needs a click (mail scanners open links, they don't press
 * buttons), and only the invited address can accept.
 */
export function InviteAccept() {
  const me = useMe();
  // undefined on the server (no window yet), then the token or null in the browser.
  const token = useSyncExternalStore(noSubscribe, () => readInviteToken(window.location.hash), () => undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const client = useQueryClient();

  useEffect(() => {
    // Read and stored already: take it out of the address bar, history and screenshots.
    if (window.location.hash) window.history.replaceState(null, "", "/invite");
  }, []);

  async function accept() {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      unwrap(await api.POST("/api/v1/invites/accept", { body: { token } }));
      clearInviteToken();
      resetWorkspaceState(client); // nothing cached from the previous workspace may show
      router.push("/home");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center bg-subtle px-4">
      <div className="w-full max-w-[440px] rounded-xl border border-border bg-bg p-6 shadow-sm">
        <Logo withWordmark />
        <div className="mt-6 flex items-center gap-2">
          <Users className="size-5 text-brand" aria-hidden="true" />
          <h1 className="text-page font-semibold text-fg">You&apos;re invited to a workspace</h1>
        </div>
        {token === undefined || me.isPending ? (
          <p role="status" className="mt-4 flex items-center gap-2 text-caption text-fg-muted">
            <Spinner className="size-4" /> Loading…
          </p>
        ) : token === null ? (
          <p className="mt-4 flex gap-2 text-body text-fg-body">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
            This invitation link is incomplete. Open it again from the email, or ask for a new one.
          </p>
        ) : !me.data ? (
          <>
            <p className="mt-4 text-body text-fg-body">Sign in, or create an account with the email address the invitation was sent to. Then come back to this page.</p>
            <div className="mt-5 flex gap-2">
              <Button asChild variant="primary">
                <Link href="/sign-in?next=/invite">Sign in</Link>
              </Button>
              <Button asChild variant="secondary">
                <Link href="/sign-up?next=/invite">Create account</Link>
              </Button>
            </div>
          </>
        ) : (
          <>
            <p className="mt-4 text-body text-fg-body">
              Join as <span className="font-medium text-fg">{me.data.user.email}</span>. You&apos;ll see only sources members choose to
              share, and everything you connect there stays private to you unless you share it.
            </p>
            {error ? (
              <p role="alert" className="mt-3 text-caption text-danger">
                {error}
              </p>
            ) : null}
            <div className="mt-5 flex gap-2">
              <Button variant="primary" onClick={() => void accept()} loading={busy}>
                Accept invitation
              </Button>
              <Button asChild variant="ghost">
                <Link href="/home">Not now</Link>
              </Button>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
