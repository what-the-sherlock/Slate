import { toast } from "sonner";

/** What POST …/sync answered (P0-2): from the sync's run, never a guess. */
export type SyncReply = {
  status: "done" | "partial" | "failed" | "background";
  reason?: string | null;
};

export type SyncMessage = { tone: "success" | "info" | "error"; text: string };

/**
 * One wording for every "Sync now" and first-sync result. A failure is never described as
 * "continuing in the background", and "background" is said only when the server could
 * vouch that the worker is keeping up.
 */
export function syncMessage(reply: SyncReply, subject = "it"): SyncMessage {
  switch (reply.status) {
    case "done":
      return { tone: "success", text: `Synced ${subject}.` };
    case "partial":
      return { tone: "info", text: reply.reason ?? `Synced part of ${subject}; the rest follows on the next sync.` };
    case "failed":
      return { tone: "error", text: `Sync failed: ${reply.reason ?? "something went wrong. Try again."}` };
    case "background":
      return { tone: "info", text: `Syncing ${subject} in the background. The source page shows progress.` };
  }
}

export function toastSync(reply: SyncReply, subject?: string): SyncMessage {
  const m = syncMessage(reply, subject);
  if (m.tone === "success") toast.success(m.text);
  else if (m.tone === "error") toast.error(m.text);
  else toast(m.text);
  return m;
}
