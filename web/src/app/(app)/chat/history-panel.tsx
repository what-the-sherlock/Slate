"use client";

import { Ellipsis, MessageSquareDashed, PanelLeftClose, Pencil, RotateCcw, Search, SquarePen, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { errorMessage } from "@/lib/api/client";
import { useChatSessions, useDeleteChat, useRenameChat, useRestoreChat, type ChatSession } from "@/lib/api/hooks";
import { groupByDay, matchesFilter } from "@/lib/chat-history";
import { cn } from "@/lib/utils";

/**
 * The chat history (UI_DESIGN §4.3): chats grouped by day, a filter, and New chat. On wide
 * screens it's a collapsible column beside the conversation; below lg it's a drawer.
 * Each row has a ⋯ menu (ADR-0044): Rename edits the title in place; Delete removes the
 * chat at once with an Undo toast (the server keeps it restorable for 24 hours).
 */
export function HistoryPanel({
  activeId,
  streaming,
  onNewChat,
  onClose,
  onNavigate,
  variant,
}: {
  activeId: string | null;
  streaming: readonly string[];
  onNewChat: () => void;
  onClose: () => void;
  onNavigate?: () => void;
  variant: "column" | "drawer";
}) {
  const sessions = useChatSessions();
  const [filter, setFilter] = useState("");
  const remove = useDeleteChat();
  const restore = useRestoreChat();
  const router = useRouter();

  // Lives in the panel, not the row: the row unmounts the moment its chat leaves the list,
  // and Undo must still work after that.
  async function deleteChat(id: string, wasActive: boolean) {
    try {
      await remove.mutateAsync(id);
    } catch (e) {
      toast.error(errorMessage(e));
      return;
    }
    if (wasActive) onNewChat();
    toast("Chat deleted", {
      duration: UNDO_MS,
      action: {
        label: "Undo",
        onClick: () => {
          restore
            .mutateAsync(id)
            .then(() => {
              if (wasActive) router.push(`/chat?c=${id}`, { scroll: false });
            })
            .catch((e: unknown) => toast.error(errorMessage(e)));
        },
      },
    });
  }
  const all = useMemo(() => sessions.data?.pages.flat() ?? [], [sessions.data]);
  const groups = useMemo(() => groupByDay(all.filter((s) => matchesFilter(s, filter))), [all, filter]);
  const sentinel = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = sessions;

  // Infinite scroll: load the next 30 when the end of the list comes into view.
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !hasNextPage || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !isFetchingNextPage) void fetchNextPage();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  return (
    <nav aria-label="Chat history" className="flex h-full w-full flex-col bg-subtle">
      <div className="flex h-14 shrink-0 items-center gap-1 border-b border-border pr-2 pl-4">
        <h2 className="flex-1 text-ui font-semibold text-fg">Chats</h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label="New chat" onClick={onNewChat}>
              <SquarePen />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            New chat <kbd className="ml-1 font-sans opacity-70">Ctrl ⇧ O</kbd>
          </TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={variant === "drawer" ? "Close chats" : "Hide chats"} onClick={onClose}>
              {variant === "drawer" ? <X /> : <PanelLeftClose />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{variant === "drawer" ? "Close" : "Hide chats"}</TooltipContent>
        </Tooltip>
      </div>

      <div className="shrink-0 px-3 pt-3 pb-1">
        <label className="flex h-8 items-center gap-2 rounded-md border border-border bg-bg px-2.5 transition-[border-color,box-shadow] duration-150 focus-within:border-brand focus-within:shadow-[0_0_0_3px_var(--sl-accent-subtle)]">
          <Search className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
          <span className="sr-only">Filter chats</span>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setFilter("")}
            placeholder="Filter chats"
            className="min-w-0 flex-1 bg-transparent text-caption text-fg outline-none placeholder:text-fg-subtle"
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {sessions.isPending ? (
          <div className="space-y-2 px-2 pt-4" aria-busy="true" aria-label="Loading chats">
            <Skeleton className="h-3 w-14" />
            {[72, 88, 60, 80, 66].map((w) => (
              <Skeleton key={w} className="h-7" style={{ width: `${w}%` }} />
            ))}
          </div>
        ) : sessions.isError ? (
          <div className="flex flex-col items-start gap-2 px-2 pt-4 text-caption text-fg-muted">
            <p>Couldn&apos;t load your chats.</p>
            <Button size="sm" variant="secondary" onClick={() => void sessions.refetch()}>
              <RotateCcw /> Try again
            </Button>
          </div>
        ) : !all.length ? (
          <div className="flex flex-col items-center gap-2 px-4 pt-10 text-center">
            <MessageSquareDashed className="size-5 text-fg-subtle" aria-hidden="true" />
            <p className="text-caption text-fg-muted">Your chats will appear here.</p>
          </div>
        ) : !groups.length ? (
          <p className="px-2 pt-4 text-caption text-fg-muted">No chats match &ldquo;{filter}&rdquo;.</p>
        ) : (
          groups.map((g) => (
            <div key={g.label} className="pt-3">
              <p className="px-2 pb-1 text-data font-medium text-fg-subtle">{g.label}</p>
              <ul className="space-y-px">
                {g.items.map((s) => (
                  <HistoryRow
                    key={s.id}
                    session={s}
                    active={s.id === activeId}
                    live={streaming.includes(s.id)}
                    onNavigate={onNavigate}
                    onDelete={() => void deleteChat(s.id, s.id === activeId)}
                  />
                ))}
              </ul>
            </div>
          ))
        )}
        <div ref={sentinel} className="h-px" />
        {isFetchingNextPage ? <Skeleton className="mx-2 mt-2 h-7 w-3/4" /> : null}
      </div>
    </nav>
  );
}

export const UNDO_MS = 8000;

function HistoryRow({
  session,
  active,
  live,
  onNavigate,
  onDelete,
}: {
  session: ChatSession;
  active: boolean;
  live: boolean;
  onNavigate?: () => void;
  onDelete: () => void;
}) {
  const title = session.title || "Untitled chat";
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const rename = useRenameChat();
  function saveTitle(next: string) {
    setEditing(false);
    const clean = next.split(/\s+/).filter(Boolean).join(" ");
    if (!clean || clean === session.title) return; // empty or unchanged: keep the old name
    rename.mutate({ id: session.id, title: clean }, { onError: (e) => toast.error(errorMessage(e)) });
  }

  function deleteChat() {
    setMenuOpen(false);
    onDelete();
  }

  if (editing) {
    return (
      <li>
        <RenameField initial={session.title ?? ""} onSave={saveTitle} onCancel={() => setEditing(false)} />
      </li>
    );
  }

  return (
    <li className="group/row relative animate-in fade-in slide-in-from-left-1 duration-200 motion-reduce:animate-none">
      <Link
        href={`/chat?c=${session.id}`}
        scroll={false}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        title={title}
        className={cn(
          "relative flex h-8 items-center gap-2 rounded-md pr-8 pl-2 text-caption transition-colors duration-150",
          active ? "bg-bg font-medium text-fg shadow-xs ring-1 ring-border" : "text-fg-muted hover:bg-muted hover:text-fg",
        )}
      >
        <span className="min-w-0 flex-1 truncate">{title}</span>
        {live ? <span className="sl-pulse shrink-0" role="status" aria-label="Answering" /> : null}
      </Link>
      <Popover open={menuOpen} onOpenChange={setMenuOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`Options for ${title}`}
            className={cn(
              "absolute top-1 right-1 inline-flex size-6 items-center justify-center rounded text-fg-subtle transition-opacity duration-150 hover:bg-muted hover:text-fg focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
              // Hidden until the row is hovered or focused; always shown where there's no
              // hover (touch), on the open chat, and while its menu is open.
              active || menuOpen ? "opacity-100" : "opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100 [@media(hover:none)]:opacity-100",
            )}
          >
            <Ellipsis className="size-3.5" aria-hidden="true" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" side="bottom" className="w-44 p-1" role="menu" aria-label={`Options for ${title}`}>
          <MenuItem
            onSelect={() => {
              setMenuOpen(false);
              setEditing(true);
            }}
          >
            <Pencil aria-hidden="true" /> Rename
          </MenuItem>
          <MenuItem danger disabled={live} hint={live ? "Wait for the answer to finish" : undefined} onSelect={deleteChat}>
            <Trash2 aria-hidden="true" /> Delete
          </MenuItem>
        </PopoverContent>
      </Popover>
    </li>
  );
}

function MenuItem({
  children,
  onSelect,
  danger = false,
  disabled = false,
  hint,
}: {
  children: React.ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      title={hint}
      onClick={onSelect}
      className={cn(
        "flex h-8 w-full items-center gap-2 rounded px-2 text-left text-caption transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:size-3.5",
        danger ? "text-danger hover:bg-danger-subtle" : "text-fg hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}

/** Inline rename: Enter or leaving the field saves, Escape cancels. */
export function RenameField({ initial, onSave, onCancel }: { initial: string; onSave: (title: string) => void; onCancel: () => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.select(), []);

  function finish(save: boolean) {
    if (done.current) return; // blur after Enter/Escape must not act twice
    done.current = true;
    if (save) onSave(value);
    else onCancel();
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  }

  return (
    <input
      ref={input}
      value={value}
      maxLength={120}
      aria-label="Chat name"
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={onKeyDown}
      onBlur={() => finish(true)}
      className="h-8 w-full rounded-md border border-brand bg-bg px-2 text-caption text-fg shadow-[0_0_0_3px_var(--sl-accent-subtle)] outline-none"
    />
  );
}
