"use client";

import Link from "next/link";
import { useState } from "react";
import { LogoTile, type ConnectorKey } from "@/components/brand";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useDiscordStatus, useGitHubStatus, useIntegrations } from "@/lib/api/hooks";
import { brandKey } from "@/lib/integrations";
import { DiscordPanel } from "./discord-panel";
import { EmailPanel } from "./email-panel";
import { GitHubPanel } from "./github-panel";
import { UploadPanel } from "./upload-panel";

const AVAILABLE: { key: ConnectorKey; name: string; description: string; ready: boolean }[] = [
  { key: "upload", name: "Upload files", description: "PDF, Word, Markdown, HTML, CSV, text or .mbox, up to 25 MB each", ready: true },
  { key: "github", name: "GitHub", description: "Docs, issues and pull requests from your repositories", ready: true },
  { key: "discord", name: "Discord", description: "Channels, threads and forum posts from your servers", ready: true },
  { key: "gmail", name: "Email", description: "Gmail, Yahoo, iCloud or Fastmail, or an .mbox export. Always private", ready: true },
];
const TITLES = {
  pick: "Add a source",
  upload: "Upload files",
  github: "Connect GitHub",
  discord: "Connect Discord",
  email: "Connect email",
} as const;
// Not built yet. OAuth providers come from the server's registry (useIntegrations), so
// this dialog and the Integrations page can't disagree.
const LATER_STATIC: { key: ConnectorKey; name: string; description: string }[] = [
  { key: "confluence", name: "Confluence", description: "Spaces and pages" },
];

type View = "pick" | "upload" | "github" | "discord" | "email";
type StartView = Exclude<View, "pick">;
const START_VIEWS: readonly StartView[] = ["upload", "github", "discord", "email"];

/** Whether a ?add= value names a connector the dialog can open at (Integrations links). */
export function isStartView(value: string): value is StartView {
  return (START_VIEWS as readonly string[]).includes(value);
}

/** "Add a source" (UI_DESIGN §4.5): available connectors, then non-interactive "Coming later". */
export function AddSourceDialog({
  open,
  onOpenChange,
  initialView,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialView?: StartView;
}) {
  const [view, setView] = useState<View>(initialView ?? "pick");
  const github = useGitHubStatus(open);
  const integrations = useIntegrations();
  const oauth = (integrations.data ?? []).filter((i) => i.auth === "oauth2" && i.state !== "coming_soon");
  const later = [
    ...(integrations.data ?? [])
      .filter((i) => i.state === "coming_soon")
      .map((i) => ({ key: brandKey(i.id) as ConnectorKey, name: i.name, description: i.tagline })),
    ...LATER_STATIC,
  ];
  const discord = useDiscordStatus(open);
  // A connector backed by a server-side integration is live only when that's configured.
  const status = { github, discord } as const;
  const isReady = (key: ConnectorKey, ready: boolean) =>
    key === "github" || key === "discord" ? status[key].data?.available === true : ready;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setView("pick");
      }}
    >
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle className="text-dialog font-semibold tracking-[-0.015em]">
            {TITLES[view]}
          </DialogTitle>
          <DialogDescription className="text-body text-fg-muted">
            {view === "pick"
              ? "Choose what Slate should search."
              : view === "upload"
                ? "Files go straight to private storage, then they're processed and indexed."
                : view === "github"
                  ? "Install Slate's GitHub App on the repositories you want to search."
                  : view === "discord"
                    ? "Add Slate's read-only bot to a server, then pick the channels to search."
                    : "Ask about any email: Slate reads your mailbox read-only and keeps it private to you."}
          </DialogDescription>
        </DialogHeader>

        {view === "upload" ? (
          <UploadPanel onBack={() => setView("pick")} />
        ) : view === "github" ? (
          <GitHubPanel onBack={() => setView("pick")} />
        ) : view === "discord" ? (
          <DiscordPanel onBack={() => setView("pick")} />
        ) : view === "email" ? (
          <EmailPanel onBack={() => setView("pick")} onImport={() => setView("upload")} />
        ) : (
          <div className="flex flex-col gap-5">
            <ul className="grid gap-3 sm:grid-cols-2">
              {AVAILABLE.map((c) => (
                <li key={c.key}>
                  <button
                    type="button"
                    disabled={!isReady(c.key, c.ready)}
                    onClick={() => {
                      if (c.key === "gmail") setView("email");
                      else if (c.key === "upload" || c.key === "github" || c.key === "discord") setView(c.key);
                    }}
                    className="flex h-full w-full flex-col gap-3 rounded-lg border border-border bg-bg p-4 text-left shadow-xs transition-[background-color,border-color,transform] hover:border-border-strong hover:bg-subtle active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-border disabled:hover:bg-bg motion-reduce:active:scale-100"
                  >
                    <LogoTile connector={c.key} />
                    <span>
                      <span className="block text-ui font-medium text-fg">{c.name}</span>
                      <span className="mt-0.5 block text-caption text-fg-subtle">{c.description}</span>
                    </span>
                    {!isReady(c.key, c.ready) ? (
                      <span className="text-data font-medium text-fg-subtle">
                        {c.key === "github" || c.key === "discord"
                          ? status[c.key].isPending
                            ? "Checking…"
                            : "Not set up on this server yet"
                          : "Arrives in a later milestone"}
                      </span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
            <section aria-labelledby="accounts">
              <h3 id="accounts" className="mb-2 text-data font-medium text-fg-subtle">
                Sign in with your account
              </h3>
              <ul className="grid gap-3 sm:grid-cols-2">
                {oauth.map((i) => {
                  const ready = i.state !== "not_set_up";
                  const body = (
                    <>
                      <LogoTile connector={brandKey(i.id)} size={32} />
                      <span className="min-w-0">
                        <span className="block text-body font-medium text-fg">{i.name}</span>
                        <span className="block truncate text-data text-fg-subtle">
                          {ready ? (i.state === "connected" ? "Connected" : i.tagline) : "Not set up on this server yet"}
                        </span>
                      </span>
                    </>
                  );
                  const tile = "flex items-center gap-3 rounded-lg border border-border p-3";
                  return (
                    <li key={i.id}>
                      {ready ? (
                        <Link
                          href={`/integrations?connect=${i.id}`}
                          className={`${tile} bg-bg transition-colors hover:border-border-strong hover:bg-subtle`}
                        >
                          {body}
                        </Link>
                      ) : (
                        <span className={`${tile} bg-subtle opacity-70`}>{body}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
            <section aria-labelledby="later">
              <h3 id="later" className="mb-2 text-data font-medium text-fg-subtle">
                Coming later
              </h3>
              <ul className="grid gap-3 sm:grid-cols-2">
                {later.map((c) => (
                  <li key={c.key} aria-label={`${c.name}, planned for a later release`} className="relative flex items-center gap-3 rounded-lg border border-border bg-subtle p-3">
                    <LogoTile connector={c.key} size={32} />
                    <span className="min-w-0">
                      <span className="block text-body font-medium text-fg-muted">{c.name}</span>
                      <span className="block truncate text-data text-fg-subtle">{c.description}</span>
                    </span>
                    <span className="ml-auto rounded-sm border border-brand-border px-2 py-0.5 text-data font-medium text-brand-strong">
                      Later
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
