"use client";

import { useState } from "react";
import { LogoTile, type ConnectorKey } from "@/components/brand";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { UploadPanel } from "./upload-panel";

const AVAILABLE: { key: ConnectorKey; name: string; description: string; ready: boolean }[] = [
  { key: "upload", name: "Upload files", description: "PDF, Word, Markdown, HTML, CSV and text, up to 25 MB each", ready: true },
  { key: "github", name: "GitHub", description: "Code, docs, issues and pull requests", ready: false },
  { key: "discord", name: "Discord", description: "Channels and threads from a server", ready: false },
  { key: "gmail", name: "Gmail", description: "Your Workspace mailbox. Always private", ready: false },
];
const LATER: { key: ConnectorKey; name: string; description: string }[] = [
  { key: "slack", name: "Slack", description: "Channels and threads" },
  { key: "notion", name: "Notion", description: "Pages and databases" },
  { key: "googledrive", name: "Google Drive", description: "Docs, Sheets and PDFs" },
  { key: "confluence", name: "Confluence", description: "Spaces and pages" },
];

/** "Add a source" (UI_DESIGN §4.5): available connectors, then non-interactive "Coming later". */
export function AddSourceDialog({
  open,
  onOpenChange,
  canPublish,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canPublish: boolean;
}) {
  const [view, setView] = useState<"pick" | "upload">("pick");
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
            {view === "pick" ? "Add a source" : "Upload files"}
          </DialogTitle>
          <DialogDescription className="text-body text-fg-muted">
            {view === "pick"
              ? "Choose what Slate should search."
              : "Files go straight to private storage, then they're processed and indexed."}
          </DialogDescription>
        </DialogHeader>

        {view === "upload" ? (
          <UploadPanel canPublish={canPublish} onBack={() => setView("pick")} />
        ) : (
          <div className="flex flex-col gap-5">
            <ul className="grid gap-3 sm:grid-cols-2">
              {AVAILABLE.map((c) => (
                <li key={c.key}>
                  <button
                    type="button"
                    disabled={!c.ready}
                    onClick={() => c.key === "upload" && setView("upload")}
                    className="flex h-full w-full flex-col gap-3 rounded-lg border border-border bg-bg p-4 text-left shadow-xs transition-[background-color,border-color,transform] hover:border-border-strong hover:bg-subtle active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-border disabled:hover:bg-bg motion-reduce:active:scale-100"
                  >
                    <LogoTile connector={c.key} />
                    <span>
                      <span className="block text-ui font-medium text-fg">{c.name}</span>
                      <span className="mt-0.5 block text-caption text-fg-subtle">{c.description}</span>
                    </span>
                    {!c.ready ? <span className="text-data font-medium text-fg-subtle">Arrives in a later milestone</span> : null}
                  </button>
                </li>
              ))}
            </ul>
            <section aria-labelledby="later">
              <h3 id="later" className="mb-2 text-data font-medium text-fg-subtle">
                Coming later
              </h3>
              <ul className="grid gap-3 sm:grid-cols-2">
                {LATER.map((c) => (
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
