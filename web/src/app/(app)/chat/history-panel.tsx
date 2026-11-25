"use client";

import { MessageSquareDashed, PanelLeftClose, RotateCcw, Search, SquarePen, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useChatSessions, type ChatSession } from "@/lib/api/hooks";
import { groupByDay, matchesFilter } from "@/lib/chat-history";
import { cn } from "@/lib/utils";

/**
 * The chat history (UI_DESIGN §4.3): chats grouped by day, a filter, and New chat. On wide
 * screens it's a collapsible column beside the conversation; below lg it's a drawer.
 * Rename and delete wait for API support: no menu that does nothing (§3.9).
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
                  <HistoryRow key={s.id} session={s} active={s.id === activeId} live={streaming.includes(s.id)} onNavigate={onNavigate} />
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

function HistoryRow({ session, active, live, onNavigate }: { session: ChatSession; active: boolean; live: boolean; onNavigate?: () => void }) {
  const title = session.title || "Untitled chat";
  return (
    <li className="animate-in fade-in slide-in-from-left-1 duration-200 motion-reduce:animate-none">
      <Link
        href={`/chat?c=${session.id}`}
        scroll={false}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        title={title}
        className={cn(
          "group relative flex h-8 items-center gap-2 rounded-md px-2 text-caption transition-colors duration-150",
          active ? "bg-bg font-medium text-fg shadow-xs ring-1 ring-border" : "text-fg-muted hover:bg-muted hover:text-fg",
        )}
      >
        <span className="min-w-0 flex-1 truncate">{title}</span>
        {live ? (
          <span className="sl-pulse shrink-0" role="status" aria-label="Answering" />
        ) : null}
      </Link>
    </li>
  );
}
