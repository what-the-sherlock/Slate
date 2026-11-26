"use client";

import { ArrowLeft, Check, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";

const READS = [
  "Channels you choose, and their message history",
  "Threads and forum posts in those channels (never private threads)",
  "Your roles in the server, once, to show only channels you can read",
];

/**
 * Step 1 of connecting Discord (ADR-0027): say exactly what Slate's bot can read, then hand
 * the user to Discord to add it to a server. The callback page does the rest.
 */
export function DiscordPanel({ onBack }: { onBack: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const { url } = unwrap(await api.POST("/api/v1/sources/discord/install"));
      window.location.assign(url);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <h3 className="text-ui font-medium text-fg">Slate&apos;s bot can read, never post:</h3>
        <ul className="flex flex-col gap-1.5">
          {READS.map((r) => (
            <li key={r} className="flex items-start gap-2 text-caption text-fg-body">
              <Check className="mt-0.5 size-3.5 shrink-0 text-success" aria-hidden="true" />
              {r}
            </li>
          ))}
        </ul>
      </section>
      <section className="rounded-md border border-border bg-subtle px-3 py-2.5 text-caption text-fg-muted">
        <p>
          <span className="font-medium text-fg">Only you can search it.</span> What you connect is private to you, not
          shared with other accounts. Private channels work too, but only ones you can read yourself.
        </p>
        <p className="mt-1.5">
          You need <span className="font-medium text-fg">Manage Server</span> to add the bot. The first sync reads the last
          90 days; after that new messages arrive with the hourly sync.
        </p>
      </section>
      {error ? (
        <p role="alert" className="text-caption text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex items-center justify-between gap-2">
        <Button variant="ghost" onClick={onBack} disabled={busy}>
          <ArrowLeft aria-hidden="true" />
          Back
        </Button>
        <Button variant="primary" onClick={() => void start()} loading={busy}>
          Add Slate to your server
          <ExternalLink aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
