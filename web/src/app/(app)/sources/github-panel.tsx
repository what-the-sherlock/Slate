"use client";

import { ArrowLeft, Check, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";

const READS = [
  "Repository contents (to read Markdown and text docs)",
  "Issues and pull requests, with their comments",
  "Repository metadata (names, visibility, default branch)",
];

/**
 * Step 1 of connecting GitHub (ADR-0023): say exactly what Slate can read, then hand the user
 * to GitHub to install the app on the repos they choose. The callback page does the rest.
 */
export function GitHubPanel({ onBack }: { onBack: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const { url } = unwrap(await api.POST("/api/v1/sources/github/install"));
      window.location.assign(url);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <h3 className="text-ui font-medium text-fg">Slate&apos;s GitHub App can read, never write:</h3>
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
          <span className="font-medium text-fg">What gets indexed:</span> README and other docs, plus issues and pull
          requests. Source code isn&apos;t indexed yet. What you connect is private to you, not shared with other accounts.
        </p>
        <p className="mt-1.5">
          On GitHub you choose which repositories the app may see. Back here, you pick which of those to index.
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
          Continue to GitHub
          <ExternalLink aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
