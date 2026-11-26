"use client";

import { Check, ChevronsUpDown, Users } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { resetWorkspaceState } from "@/lib/workspace-reset";
import { api, errorMessage } from "@/lib/api/client";
import { useWorkspaces } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";

/**
 * The active workspace and the others this account belongs to (ADR-0030). Switching reloads
 * the app, because every cached query belongs to one workspace; a stale list must never show
 * another workspace's data.
 */
export function WorkspaceSwitcher({ collapsed }: { collapsed: boolean }) {
  const spaces = useWorkspaces();
  const client = useQueryClient();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const list = spaces.data ?? [];
  const active = list.find((w) => w.active);
  if (!active || (list.length < 2 && active.members < 2)) return null; // alone: nothing to switch or show

  async function choose(id: string) {
    if (id === active?.id) return;
    setBusy(true);
    try {
      const r = await api.POST("/api/v1/workspaces/switch", { body: { workspace_id: id } });
      if (!r.response.ok) throw new Error("Couldn't switch workspace");
      resetWorkspaceState(client); // drop every cached query from the old workspace
      router.push("/home");
      setBusy(false);
    } catch (err) {
      toast.error(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={busy}
          aria-label={`Workspace: ${active.name}. Switch workspace`}
          className={cn(
            "flex w-full items-center gap-2 rounded-md border border-border px-2 py-1.5 text-left text-caption text-fg hover:bg-subtle",
            collapsed && "justify-center px-1",
          )}
        >
          <Users className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
          {collapsed ? null : (
            <>
              <span className="min-w-0 flex-1 truncate font-medium">{active.name}</span>
              <ChevronsUpDown className="size-3 shrink-0 text-fg-subtle" aria-hidden="true" />
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent side="bottom" align="start" className="w-64">
        <p className="px-2 pt-1.5 pb-1 text-label font-semibold tracking-wider text-fg-subtle uppercase">Workspaces</p>
        <ul role="listbox" aria-label="Workspaces">
          {list.map((w) => (
            <li key={w.id}>
              <button
                type="button"
                role="option"
                aria-selected={w.active}
                onClick={() => void choose(w.id)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-caption text-fg hover:bg-muted"
              >
                <span className="inline-flex size-4 items-center justify-center">{w.active ? <Check className="size-3.5 text-brand" /> : null}</span>
                <span className="min-w-0 flex-1 truncate">{w.name}</span>
                <span className="text-data text-fg-subtle capitalize">{w.role}</span>
              </button>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
