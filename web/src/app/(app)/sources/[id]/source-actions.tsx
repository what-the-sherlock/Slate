"use client";

import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys, type Source } from "@/lib/api/hooks";

/** "Sync now" for connector sources (UI_DESIGN §4.5). Uploads have nothing to sync. */
export function SyncNowButton({ source }: { source: Source }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  if (source.connector !== "github") return null;
  const running = source.status === "syncing" || source.status === "queued";

  async function sync() {
    setBusy(true);
    try {
      const res = unwrap(await api.POST("/api/v1/sources/{source_id}/sync", { params: { path: { source_id: source.id } } }));
      toast.success(res.status === "done" ? "Synced" : "Sync started. It finishes in the background within the hour.");
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: keys.sources });
      void client.invalidateQueries({ queryKey: keys.runs(source.id) });
    }
  }

  return (
    <Button variant="secondary" onClick={() => void sync()} loading={busy} disabled={running && !busy}>
      {busy ? null : <RefreshCw aria-hidden="true" />}
      {busy ? "Syncing…" : running ? "Syncing" : "Sync now"}
    </Button>
  );
}

/** Danger zone: disconnect a source and delete everything indexed from it. */
export function DangerZone({ source, canManage }: { source: Source; canManage: boolean }) {
  const router = useRouter();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!canManage || source.connector === "upload") return null;

  async function disconnect() {
    setBusy(true);
    try {
      const r = await api.DELETE("/api/v1/sources/{source_id}", { params: { path: { source_id: source.id } } });
      if (!r.response.ok) unwrap(r);
      toast.success(`${source.name} disconnected`);
      await client.invalidateQueries({ queryKey: keys.sources });
      router.replace("/sources");
    } catch (err) {
      toast.error(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <section className="mx-4 mt-8 rounded-lg border border-danger-border px-4 py-4 sm:mx-8" aria-labelledby="danger">
      <h2 id="danger" className="text-ui font-semibold text-fg">
        Danger zone
      </h2>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <p className="flex-1 text-caption text-fg-muted">
          Disconnecting removes this repository and all {source.documents.toLocaleString()} indexed documents from Slate.
          Nothing changes on GitHub.
        </p>
        <Button variant="secondary" className="text-danger" onClick={() => setOpen(true)}>
          <Trash2 aria-hidden="true" />
          Disconnect
        </Button>
      </div>
      <Dialog open={open} onOpenChange={(o) => !busy && setOpen(o)}>
        <DialogContent className="sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle>Disconnect {source.name}?</DialogTitle>
            <DialogDescription>
              Its documents, issues and pull requests are deleted from Slate, and answers stop citing them. You can connect it
              again later; it will be indexed from scratch.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" className="bg-danger hover:bg-danger" onClick={() => void disconnect()} loading={busy}>
              Disconnect
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
