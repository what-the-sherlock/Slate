"use client";

import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { ArrowDown, MessageSquareX, PanelLeftOpen, SquarePen } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { keys, useChatMessages, useChatSessions, useSources, type ChatSession } from "@/lib/api/hooks";
import { isBusy, type Turn } from "@/lib/chat";
import { chatTitle } from "@/lib/chat-history";
import { chatStore, useStreamingChats, useThread, type AskOptions } from "@/lib/chat-store";
import { useStickToBottom } from "@/lib/use-stick-to-bottom";
import { cn } from "@/lib/utils";
import { ChatEmpty } from "./chat-empty";
import { Composer, LIMIT } from "./composer";
import { HistoryPanel } from "./history-panel";
import { SourcesPanel } from "./sources-panel";
import { TurnView, type TurnActions } from "./turn";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// A new chat lives under a draft key until the server names its session (chat-store.ts).
let drafts = 0;
const nextDraft = () => `draft:${++drafts}`;

// The history column's open/closed state is a preference kept in localStorage.
const HISTORY_KEY = "slate.chats.open";
const historyListeners = new Set<() => void>();
function subscribeHistory(cb: () => void) {
  historyListeners.add(cb);
  window.addEventListener("storage", cb);
  return () => {
    historyListeners.delete(cb);
    window.removeEventListener("storage", cb);
  };
}
function useHistoryOpen(): [boolean, (open: boolean) => void] {
  const open = useSyncExternalStore(
    subscribeHistory,
    () => window.localStorage.getItem(HISTORY_KEY) !== "0",
    () => true,
  );
  return [
    open,
    (next) => {
      window.localStorage.setItem(HISTORY_KEY, next ? "1" : "0");
      historyListeners.forEach((cb) => cb());
    },
  ];
}

/**
 * Chat (UI_DESIGN §4.3): history | conversation + composer | sources panel.
 *
 * The page is exactly the viewport tall and scrolls only the conversation, so the composer
 * stays pinned at the bottom whatever the length of the chat. Turns come from the chat
 * store (lib/chat-store.ts), which keeps streams alive across chat switches.
 */
export function ChatView() {
  const router = useRouter();
  const params = useSearchParams();
  const client = useQueryClient();
  const raw = params.get("c");
  const urlSession = raw && UUID_RE.test(raw) ? raw : null;

  // "New chat" (the URL lost ?c=) gets a fresh draft key; adjusting state while rendering
  // is React's pattern for "reset when a prop changes", with no flash of the old chat.
  const [draftKey, setDraftKey] = useState(nextDraft);
  const [prevUrl, setPrevUrl] = useState(urlSession);
  if (prevUrl !== urlSession) {
    setPrevUrl(urlSession);
    if (!urlSession) setDraftKey(nextDraft());
  }
  const key = urlSession ?? draftKey;
  const keyRef = useRef(key);
  useEffect(() => {
    keyRef.current = key;
  }, [key]);

  const thread = useThread(key);
  const turns = useMemo(() => thread?.turns ?? [], [thread]);
  const streamingChats = useStreamingChats();
  const history = useChatMessages(urlSession && !thread ? urlSession : null);
  const sessions = useChatSessions();
  const sources = useSources();

  const [draft, setDraft] = useState("");
  const [composerError, setComposerError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  // The sources panel belongs to one chat: switching chats closes it (no effect needed).
  const [panelState, setPanelState] = useState<{ thread: string; turn: string; n: number | null } | null>(null);
  const panel = panelState?.thread === key ? panelState : null;
  const setPanel = useCallback((p: { turn: string; n: number | null } | null) => setPanelState(p && { thread: keyRef.current, ...p }), []);
  const [historyOpen, setHistoryOpen] = useHistoryOpen();
  const [drawer, setDrawer] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const { atBottom, scrollToBottom } = useStickToBottom(scrollRef, contentRef);

  const threadBusy = turns.some(isBusy);

  // A reopened chat: load it from the server once, unless this tab already holds it.
  useEffect(() => {
    if (urlSession && history.data) chatStore.hydrate(urlSession, history.data);
  }, [urlSession, history.data]);

  // Switching chats (or a reopened chat arriving) lands on its latest turn.
  const hasTurns = turns.length > 0;
  useEffect(() => {
    if (hasTurns) scrollToBottom("instant");
  }, [key, hasTurns, scrollToBottom]);

  // Leaving the page mid-answer would cut the stream: ask first (closing the tab only).
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (chatStore.isStreaming()) e.preventDefault();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, []);

  const newChat = useCallback(() => {
    setDrawer(false);
    setComposerError(null);
    router.push("/chat", { scroll: false });
    requestAnimationFrame(() => input.current?.focus());
  }, [router]);

  // "/" focuses the composer; Ctrl/⌘ + Shift + O starts a new chat (UI_DESIGN §3.9).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable);
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        input.current?.focus();
      } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newChat]);

  const options = useCallback(
    (startedOn: string): AskOptions => ({
      sourceIds: selected,
      onSession: (sid, question) => {
        // The new chat goes to the top of the history list at once (no refetch, no poll).
        client.setQueryData<InfiniteData<ChatSession[], string | null>>(keys.chatSessions, (old) => {
          const row: ChatSession = { id: sid, title: chatTitle(question), created_at: new Date().toISOString() };
          if (!old) return { pages: [[row]], pageParams: [null] };
          const [first = [], ...rest] = old.pages;
          return { ...old, pages: [[row, ...first.filter((s) => s.id !== sid)], ...rest] };
        });
        if (keyRef.current === startedOn) router.replace(`/chat?c=${sid}`, { scroll: false });
      },
      onRefused: (question, err) => {
        if (keyRef.current === startedOn || keyRef.current === chatStore.get(startedOn)?.key) {
          setDraft(question);
          setComposerError(err.detail);
          input.current?.focus();
        } else {
          toast.error(err.detail);
        }
      },
    }),
    [client, router, selected],
  );

  function send(text: string = draft) {
    const q = text.trim();
    if (!q || q.length > LIMIT || threadBusy) return;
    setDraft("");
    setComposerError(null);
    scrollToBottom("smooth");
    void chatStore.ask(key, q, options(key));
  }

  const actions: TurnActions = useMemo(
    () => ({
      onOpenSource: (turn: Turn, n: number | null) => setPanel({ turn: turn.key, n }),
      onRegenerate: (turn: Turn) => {
        scrollToBottom("smooth");
        void chatStore.regenerate(key, turn, options(key));
      },
      onEdit: (turn: Turn, question: string) => {
        scrollToBottom("smooth");
        void chatStore.edit(key, turn, question, options(key));
      },
      onRephrase: (question: string) => {
        setDraft(question);
        requestAnimationFrame(() => {
          const el = input.current;
          el?.focus();
          el?.setSelectionRange(el.value.length, el.value.length);
        });
      },
      onClearFilters: () => setSelected([]),
    }),
    [key, options, scrollToBottom, setPanel],
  );

  const panelTurn = panel ? turns.find((t) => t.key === panel.turn) : undefined;
  const loading = !!urlSession && !thread && history.isPending;
  // The API answers an unknown or someone else's chat with no messages (no probing).
  const missing = !!urlSession && ((!thread && history.isError) || (history.data?.length === 0 && !turns.length));
  const title =
    (urlSession && sessions.data?.pages.flat().find((s) => s.id === urlSession)?.title) || turns[0]?.question || (urlSession ? "Chat" : "New chat");

  const historyProps = {
    activeId: urlSession,
    streaming: streamingChats,
    onNewChat: newChat,
  };

  return (
    <div className="flex h-[calc(100svh-3.5rem)] min-h-0 lg:h-svh">
      {/* History: a column on wide screens, a drawer below lg. */}
      <aside
        aria-hidden={!historyOpen || undefined}
        className={cn(
          "hidden shrink-0 overflow-hidden border-border transition-[width] duration-300 ease-[var(--sl-ease-out)] motion-reduce:transition-none lg:block",
          historyOpen ? "w-[260px] border-r" : "w-0",
        )}
        inert={!historyOpen || undefined}
      >
        <div className="h-full w-[260px]">
          <HistoryPanel {...historyProps} variant="column" onClose={() => setHistoryOpen(false)} />
        </div>
      </aside>
      {drawer ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Chats">
          <button className="absolute inset-0 bg-[var(--sl-overlay)] animate-in fade-in duration-200" aria-label="Close chats" onClick={() => setDrawer(false)} />
          <div className="absolute inset-y-0 left-0 w-[280px] max-w-[85vw] border-r border-border shadow-lg animate-in slide-in-from-left duration-300 ease-[var(--sl-ease-out)]">
            <HistoryPanel {...historyProps} variant="drawer" onClose={() => setDrawer(false)} onNavigate={() => setDrawer(false)} />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-1 border-b border-border px-3 sm:px-4">
          <HeaderButton label="Show chats" className={cn(historyOpen && "lg:hidden")} onClick={() => (window.matchMedia("(min-width: 1024px)").matches ? setHistoryOpen(true) : setDrawer(true))}>
            <PanelLeftOpen />
          </HeaderButton>
          <h1 key={title} className="min-w-0 flex-1 truncate px-1.5 text-ui font-medium text-fg animate-in fade-in duration-300">
            {title}
          </h1>
          <HeaderButton label="New chat" hint="Ctrl ⇧ O" className={cn(historyOpen && "lg:hidden")} onClick={newChat}>
            <SquarePen />
          </HeaderButton>
        </header>

        <div ref={scrollRef} className="relative min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]" tabIndex={-1}>
          <div ref={contentRef} className="mx-auto flex min-h-full w-full max-w-[760px] flex-col px-4 sm:px-6">
            {missing ? (
              <MissingChat onNewChat={newChat} />
            ) : loading ? (
              <ConversationSkeleton />
            ) : !turns.length ? (
              <ChatEmpty sources={sources.data ?? []} loading={sources.isPending} onPick={actions.onRephrase} />
            ) : (
              <div className="flex flex-col gap-10 pt-8 pb-10">
                {turns.map((t, i) => (
                  <TurnView key={t.key} turn={t} isLast={i === turns.length - 1} threadBusy={threadBusy} filtersActive={selected.length > 0} actions={actions} />
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="relative shrink-0 px-4 pb-[max(12px,env(safe-area-inset-bottom))] sm:px-6">
          {/* Fade the conversation out under the composer so text never collides with it. */}
          <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 -top-8 h-8 bg-gradient-to-t from-bg to-transparent" />
          <div className="relative mx-auto max-w-[712px]">
            {!atBottom && turns.length ? (
              <button
                type="button"
                onClick={() => scrollToBottom("smooth")}
                className="absolute -top-12 left-1/2 z-10 inline-flex h-8 -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-bg px-3 text-caption font-medium text-fg-muted shadow-md transition-colors animate-in fade-in slide-in-from-bottom-2 duration-200 hover:text-fg"
              >
                <ArrowDown className="size-3.5" aria-hidden="true" />
                {threadBusy ? "New text below" : "Jump to latest"}
              </button>
            ) : null}
            <Composer
              value={draft}
              onChange={(v) => {
                setDraft(v);
                if (composerError) setComposerError(null);
              }}
              onSubmit={() => send()}
              onStop={() => chatStore.stop(key)}
              onRecall={() => turns[turns.length - 1]?.question}
              streaming={threadBusy}
              error={composerError}
              inputRef={input}
              sources={sources.data ?? []}
              selected={selected}
              onSelected={setSelected}
            />
            <p className="mt-2 text-center text-data text-fg-subtle max-sm:hidden">
              Slate can make mistakes. Check answers against their cited sources.
            </p>
          </div>
        </div>
      </div>

      {panel && panelTurn ? (
        <SourcesPanel
          sources={panelTurn.sources}
          cited={panelTurn.cited}
          selected={panel.n}
          onSelect={(n) => setPanel({ turn: panel.turn, n })}
          onClose={() => setPanel(null)}
        />
      ) : null}
    </div>
  );
}

function HeaderButton({
  label,
  hint,
  className,
  onClick,
  children,
}: {
  label: string;
  hint?: string;
  className?: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} onClick={onClick} className={className}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {label}
        {hint ? <kbd className="ml-1 font-sans opacity-70">{hint}</kbd> : null}
      </TooltipContent>
    </Tooltip>
  );
}

function ConversationSkeleton() {
  return (
    <div className="flex flex-col gap-10 pt-8" aria-busy="true" aria-label="Loading chat">
      {[0, 1].map((i) => (
        <div key={i} className="flex flex-col gap-4">
          <Skeleton className="ml-auto h-10 w-2/5 rounded-2xl" />
          <div className="space-y-2">
            <Skeleton className="h-4 w-11/12" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/5" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** A stale or someone else's chat link: same message for both (no probing). */
function MissingChat({ onNewChat }: { onNewChat: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 py-16 text-center">
      <span className="inline-flex size-10 items-center justify-center rounded-md border border-border">
        <MessageSquareX className="size-5 text-fg-muted" aria-hidden="true" />
      </span>
      <p className="text-ui font-medium text-fg">This chat isn&apos;t available</p>
      <p className="max-w-[360px] text-body text-fg-subtle">It may have been removed, or the link is wrong.</p>
      <Button variant="secondary" size="sm" onClick={onNewChat}>
        <SquarePen /> Start a new chat
      </Button>
    </div>
  );
}
