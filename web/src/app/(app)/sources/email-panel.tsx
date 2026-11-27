"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ExternalLink, KeyRound, Mail } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { toastSync } from "@/lib/sync-result";
import { keys, useMailStatus, type MailProvider } from "@/lib/api/hooks";
import { guessProvider, looksLikeAddress, normaliseAppPassword } from "@/lib/mail";

const READS = [
  "Inbox and Sent, read-only: nothing is marked read, moved or deleted",
  "Never Spam or Trash; attachments by file name only",
  "The last 90 days first (up to 2,000 conversations), then new mail hourly",
];

/**
 * Connect a mailbox over IMAP with an app password (ADR-0028), or fall back to an .mbox
 * import. The password is sent once, to Slate's API, and stored encrypted.
 */
export function EmailPanel({ onBack, onImport }: { onBack: () => void; onImport: () => void }) {
  const status = useMailStatus();
  const router = useRouter();
  const client = useQueryClient();
  const ids = { address: useId(), password: useId(), provider: useId() };
  const [address, setAddress] = useState("");
  const [password, setPassword] = useState("");
  const [chosen, setChosen] = useState<MailProvider["id"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const providers = status.data?.providers ?? [];
  const provider = chosen ?? guessProvider(address, providers) ?? "gmail";
  const info = providers.find((p) => p.id === provider);
  const ready = looksLikeAddress(address) && normaliseAppPassword(password).length >= 8;

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const done = unwrap(
        await api.POST("/api/v1/sources/email/connect", {
          body: { provider, address: address.trim(), app_password: password },
        }),
      );
      setPassword("");
      void client.invalidateQueries({ queryKey: keys.sources });
      // The first sync runs right away (up to ~90 s); big mailboxes continue in the background.
      try {
        const res = unwrap(await api.POST("/api/v1/sources/{source_id}/sync", { params: { path: { source_id: done.source_id } } }));
        toastSync(res, "your mailbox");
      } catch (err) {
        toast.error(`Mailbox connected, but the first sync failed: ${errorMessage(err)}`);
      }
      router.push(`/sources/${done.source_id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        if (ready && !busy) void connect();
      }}
    >
      <ul className="flex flex-col gap-1.5">
        {READS.map((r) => (
          <li key={r} className="flex items-start gap-2 text-caption text-fg-body">
            <Check className="mt-0.5 size-3.5 shrink-0 text-success" aria-hidden="true" />
            {r}
          </li>
        ))}
      </ul>

      {status.data && !status.data.available ? (
        <p role="status" className="rounded-md border border-border bg-subtle px-3 py-2 text-caption text-fg-muted">
          Live mail isn&apos;t set up on this server yet. You can still import an export file below.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          <label htmlFor={ids.address} className="flex flex-col gap-1 text-caption font-medium text-fg">
            Email address
            <span className="flex h-10 items-center gap-2 rounded-md border border-border-input bg-bg px-3 focus-within:border-brand">
              <Mail className="size-4 text-fg-subtle" aria-hidden="true" />
              <input
                id={ids.address}
                type="email"
                autoComplete="email"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder="you@gmail.com"
                className="min-w-0 flex-1 bg-transparent text-body font-normal outline-none"
              />
            </span>
          </label>

          <label htmlFor={ids.provider} className="flex flex-col gap-1 text-caption font-medium text-fg">
            Provider
            <select
              id={ids.provider}
              value={provider}
              onChange={(e) => setChosen(e.target.value as MailProvider["id"])}
              className="h-10 rounded-md border border-border-input bg-bg px-3 text-body font-normal"
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>

          <label htmlFor={ids.password} className="flex flex-col gap-1 text-caption font-medium text-fg">
            App password
            <span className="flex h-10 items-center gap-2 rounded-md border border-border-input bg-bg px-3 focus-within:border-brand">
              <KeyRound className="size-4 text-fg-subtle" aria-hidden="true" />
              <input
                id={ids.password}
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="abcd efgh ijkl mnop"
                className="min-w-0 flex-1 bg-transparent font-mono text-body font-normal outline-none"
              />
            </span>
            <span className="font-normal text-fg-subtle">
              Not your normal password: an app password made for Slate (needs 2-Step Verification).{" "}
              {info ? (
                <a href={info.app_password_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
                  Create one for {info.name} <ExternalLink className="size-3" aria-hidden="true" />
                </a>
              ) : null}
            </span>
          </label>

          <p className="rounded-md border border-border bg-subtle px-3 py-2 text-caption text-fg-muted">
            <span className="font-medium text-fg">Only you can search your mail.</span> The app password is stored
            encrypted and used only to read. Revoke it at your provider any time; disconnecting deletes it and everything
            indexed.
          </p>
        </div>
      )}

      {error ? (
        <p role="alert" className="text-caption text-danger">
          {error}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button type="button" variant="ghost" onClick={onBack} disabled={busy}>
          <ArrowLeft aria-hidden="true" />
          Back
        </Button>
        <span className="flex items-center gap-2">
          <Button type="button" variant="secondary" onClick={onImport} disabled={busy}>
            Import .mbox instead
          </Button>
          {status.data?.available !== false ? (
            <Button type="submit" variant="primary" disabled={!ready} loading={busy}>
              Connect mailbox
            </Button>
          ) : null}
        </span>
      </div>
    </form>
  );
}
