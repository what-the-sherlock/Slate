"use client";

import { Clock, FileText, Search as SearchIcon, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent } from "react";
import { LogoTile } from "@/components/brand";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { PanelLabel } from "@/components/panel";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api/client";
import { useDocument, useSearch, useSources, type SearchBody, type SearchHit } from "@/lib/api/hooks";
import { absoluteTime, relativeTime } from "@/lib/format";
import {
  clearRecent,
  DATE_PRESETS,
  highlight,
  MAX_FROM_CHARS,
  parseSearchState,
  pushRecent,
  queryTerms,
  recentStore,
  snippet,
  toSearchParams,
  updatedAfter,
  type DatePreset,
  type SearchState,
} from "@/lib/search";
import { cn } from "@/lib/utils";

const EXAMPLES = ["How many days of paid leave do we get?", "expense approval limit", "ERR_BUDGET"];

/** "From: …": only documents by this person (an email sender, an issue author). Applied on
 * Enter or blur, so typing doesn't fire a search per keystroke. */
function FromFilter({ value, onCommit }: { value: string; onCommit: (from: string) => void }) {
  const [draft, setDraft] = useState(value);
  const commit = () => {
    const next = draft.trim().slice(0, MAX_FROM_CHARS);
    if (next !== value) onCommit(next);
  };
  return (
    <label className={cn(chip(!!value), "gap-1 pr-1")}>
      <span className="text-fg-subtle">From</span>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          }
        }}
        placeholder="anyone"
        aria-label="From (name or email)"
        maxLength={MAX_FROM_CHARS}
        className="w-28 bg-transparent text-caption text-fg outline-none placeholder:text-fg-subtle"
      />
      {value ? (
        <button type="button" aria-label="Clear sender" onClick={() => onCommit("")} className="inline-flex">
          <X className="size-3" aria-hidden="true" />
        </button>
      ) : null}
    </label>
  );
}

export function SearchView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const state = useMemo(() => parseSearchState(new URLSearchParams(params.toString())), [params]);
  const [draft, setDraft] = useState(state.q);
  const [selected, setSelected] = useState(0);
  const recent = useSyncExternalStore(recentStore.subscribe, recentStore.getSnapshot, recentStore.getServerSnapshot);
  const input = useRef<HTMLInputElement>(null);
  const sources = useSources();

  // The URL is the source of truth (shareable, back/forward works). When it changes, the
  // field follows and the selection resets: adjusted during render, not in an effect.
  const urlKey = params.toString();
  const [seenUrl, setSeenUrl] = useState(urlKey);
  if (seenUrl !== urlKey) {
    setSeenUrl(urlKey);
    setDraft(state.q);
    setSelected(0);
  }

  // "/" focuses the field from anywhere on the page, unless the user is typing elsewhere.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === "/" && !(t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)))) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const body: SearchBody | null = state.q
    ? {
        query: state.q,
        limit: 20,
        source_ids: state.sources,
        updated_after: updatedAfter(state.date) ?? null,
        author: state.from || null,
      }
    : null;
  const search = useSearch(body);
  const hits = search.data?.hits ?? [];
  const terms = useMemo(() => queryTerms(state.q), [state.q]);
  const current = hits[selected] ?? null;

  const navigate = (next: SearchState) => {
    const qs = toSearchParams(next).toString();
    router.push(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };
  const submit = (q: string) => {
    const query = q.trim();
    if (query) pushRecent(query);
    navigate({ ...state, q: query });
  };
  const filtersActive = state.sources.length > 0 || state.date !== "any" || !!state.from;

  const onListKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((i) => Math.min(i + 1, hits.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (e.key === "Escape") {
      input.current?.focus();
    }
  };

  return (
    <>
      <PageHeader title="Search">
        <div className="px-4 pb-4 sm:px-8">
          <form
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              submit(draft);
            }}
            className="relative"
          >
            <SearchIcon className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
            <input
              ref={input}
              type="search"
              autoFocus
              value={draft}
              maxLength={500}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown" && hits.length) {
                  e.preventDefault();
                  document.getElementById("search-results")?.focus();
                }
              }}
              placeholder="Search documents you can access"
              aria-label="Search"
              className="h-11 w-full rounded-md border border-border-strong bg-bg pr-11 pl-10 text-[16px] text-fg outline-none placeholder:text-fg-subtle focus-visible:border-brand-border focus-visible:ring-3 focus-visible:ring-brand-subtle [&::-webkit-search-cancel-button]:hidden"
            />
            {draft ? (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setDraft("");
                  navigate({ ...state, q: "" });
                  input.current?.focus();
                }}
                className="absolute top-1/2 right-2.5 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-sm text-fg-subtle hover:bg-subtle hover:text-fg"
              >
                <X className="size-4" aria-hidden="true" />
              </button>
            ) : (
              <kbd className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 rounded-sm border border-border px-1.5 font-mono text-data text-fg-subtle">
                /
              </kbd>
            )}
          </form>
          <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="Filters">
            <select
              aria-label="Date"
              value={state.date}
              onChange={(e) => navigate({ ...state, date: e.target.value as DatePreset })}
              className={chip(state.date !== "any")}
            >
              {Object.entries(DATE_PRESETS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v.label}
                </option>
              ))}
            </select>
            {(sources.data ?? []).slice(0, 8).map((s) => {
              const on = state.sources.includes(s.id);
              return (
                <button
                  key={s.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    navigate({
                      ...state,
                      sources: on ? state.sources.filter((x) => x !== s.id) : [...state.sources, s.id],
                    })
                  }
                  className={chip(on)}
                >
                  <LogoTile connector={s.connector} size={16} />
                  {s.name}
                  <span className="text-fg-subtle tabular-nums">{s.documents}</span>
                  {on ? <X className="size-3" aria-hidden="true" /> : null}
                </button>
              );
            })}
            <FromFilter key={state.from} value={state.from} onCommit={(from) => navigate({ ...state, from })} />
            {filtersActive ? (
              <button type="button" onClick={() => navigate({ q: state.q, sources: [], date: "any", from: "" })} className="text-caption text-fg-muted underline-offset-2 hover:text-fg hover:underline">
                Clear all
              </button>
            ) : null}
          </div>
        </div>
      </PageHeader>

      {!state.q ? (
        <BeforeSearch
          recent={recent}
          onPick={(q) => {
            setDraft(q);
            submit(q);
          }}
          onClear={clearRecent}
        />
      ) : search.isError && !search.data ? (
        <p role="alert" className="mx-4 mt-6 rounded-md border border-danger-border bg-danger-subtle px-3 py-2.5 text-caption text-danger sm:mx-8">
          {errorMessage(search.error)}{" "}
          <button className="font-medium underline" onClick={() => search.refetch()}>
            Retry
          </button>
        </p>
      ) : search.isPending ? (
        <ResultsSkeleton />
      ) : hits.length === 0 ? (
        <EmptyState
          icon={SearchIcon}
          title={`No results for "${state.q}"`}
          action={
            filtersActive ? (
              <Button variant="secondary" onClick={() => navigate({ q: state.q, sources: [], date: "any", from: "" })}>
                Clear filters
              </Button>
            ) : undefined
          }
        >
          Try fewer words, a different spelling, or removing filters.
        </EmptyState>
      ) : (
        <div className="grid min-[1280px]:grid-cols-[minmax(0,1fr)_480px]">
          <div className={cn("min-w-0 transition-opacity", search.isPlaceholderData && "opacity-60")} aria-busy={search.isFetching}>
            <ResultMeta count={hits.length} ms={search.data?.elapsedMs} lanes={search.data?.lanes ?? []} notices={search.data?.notices ?? []} />
            <ul
              id="search-results"
              tabIndex={0}
              role="listbox"
              aria-label="Search results"
              aria-activedescendant={current ? `hit-${current.chunk_id}` : undefined}
              onKeyDown={onListKey}
              className="px-2 pb-12 outline-none sm:px-6"
            >
              {hits.map((h, i) => (
                <ResultRow
                  key={h.chunk_id}
                  hit={h}
                  terms={terms}
                  index={i}
                  selected={i === selected}
                  onSelect={() => setSelected(i)}
                />
              ))}
            </ul>
          </div>
          {current ? <PreviewPanel hit={current} terms={terms} /> : null}
        </div>
      )}
    </>
  );
}

function chip(active: boolean) {
  return cn(
    "inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-caption transition-colors",
    active ? "border-brand-border bg-brand-subtle text-brand-strong" : "border-border-strong bg-bg text-fg-muted hover:text-fg",
  );
}

function Marked({ text, terms }: { text: string; terms: string[] }) {
  // Text nodes only: retrieved content is untrusted and is never rendered as HTML.
  return (
    <>
      {highlight(text, terms).map((s, i) =>
        s.mark ? (
          <mark key={i} className="rounded-[2px] bg-mark px-px text-fg">
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}

function ResultMeta({ count, ms, lanes, notices }: { count: number; ms?: number; lanes: string[]; notices: string[] }) {
  return (
    <div className="px-4 pt-4 pb-2 sm:px-8">
      <p className="text-caption text-fg-subtle tabular-nums" role="status" aria-live="polite">
        {count} {count === 1 ? "result" : "results"}
        {ms !== undefined ? ` · ${ms} ms` : ""}
        {lanes.length === 1 && lanes[0] === "keyword" && notices.length === 0 ? " · keyword matches" : ""}
      </p>
      {notices.map((n) => (
        <p key={n} className="mt-2 rounded-md border border-warning-border bg-warning-subtle px-3 py-2 text-caption text-warning">
          {n}
        </p>
      ))}
    </div>
  );
}

function ResultRow({
  hit,
  terms,
  index,
  selected,
  onSelect,
}: {
  hit: SearchHit;
  terms: string[];
  index: number;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li
      id={`hit-${hit.chunk_id}`}
      role="option"
      aria-selected={selected}
      onClick={onSelect}
      style={{ animationDelay: `${Math.min(index, 8) * 30}ms` }}
      className={cn(
        "relative cursor-pointer rounded-md px-4 py-3 transition-colors animate-in fade-in-0 slide-in-from-bottom-1 fill-mode-both motion-reduce:animate-none",
        selected ? "bg-muted before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-brand" : "hover:bg-subtle",
      )}
    >
      <div className="flex items-center gap-2 text-data text-fg-subtle">
        <FileText className="size-3.5" aria-hidden="true" />
        <span className="truncate">{hit.heading_path ? `${hit.title} › ${hit.heading_path}` : hit.title}</span>
        <span className="ml-auto shrink-0 uppercase tracking-wide">{hit.matched_by.join(" + ")}</span>
      </div>
      <p className="mt-0.5 truncate text-[15px] font-medium text-fg">{hit.title}</p>
      <p className="mt-0.5 line-clamp-2 text-body text-fg-body">
        <Marked text={snippet(hit.content, terms)} terms={terms} />
      </p>
    </li>
  );
}

function PreviewPanel({ hit, terms }: { hit: SearchHit; terms: string[] }) {
  const doc = useDocument(hit.document_id);
  const focus = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    focus.current?.scrollIntoView?.({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [doc.data, hit.chunk_id]);

  return (
    <aside aria-label="Preview" className="border-t border-border min-[1280px]:sticky min-[1280px]:top-0 min-[1280px]:h-[calc(100svh-0px)] min-[1280px]:overflow-y-auto min-[1280px]:border-t-0 min-[1280px]:border-l">
      <div className="border-b border-border px-6 py-4">
        <PanelLabel>Preview</PanelLabel>
        <h2 className="mt-1 text-[15px] font-semibold text-fg">{hit.title}</h2>
        {doc.data ? (
          <p className="text-data text-fg-subtle">
            {doc.data.source_name} · {doc.data.chunks.length} passages
            {doc.data.source_updated_at ? (
              <>
                {" · "}
                <time dateTime={doc.data.source_updated_at} title={absoluteTime(doc.data.source_updated_at)}>
                  updated {relativeTime(doc.data.source_updated_at)}
                </time>
              </>
            ) : null}
          </p>
        ) : null}
      </div>
      <div className="px-6 py-4">
        {doc.isPending ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        ) : doc.isError ? (
          <p role="alert" className="text-caption text-danger">
            {errorMessage(doc.error)}
          </p>
        ) : (
          doc.data.chunks.map((c) => {
            const isHit = c.ordinal === hit.ordinal;
            return (
              <div
                key={c.ordinal}
                ref={isHit ? focus : undefined}
                className={cn("mb-3 rounded-md px-3 py-2 text-body whitespace-pre-wrap text-fg-body", isHit && "bg-subtle ring-1 ring-brand-border")}
              >
                {c.heading_path ? <PanelLabel className="mb-1 block">{c.heading_path}</PanelLabel> : null}
                <Marked text={c.content} terms={isHit ? terms : []} />
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

function BeforeSearch({ recent, onPick, onClear }: { recent: string[]; onPick: (q: string) => void; onClear: () => void }) {
  return (
    <div className="grid gap-8 px-4 py-6 sm:px-8 lg:grid-cols-2">
      <section>
        <div className="mb-3 flex h-5 items-center justify-between">
          <PanelLabel>Recent searches</PanelLabel>
          {recent.length ? (
            <button className="text-caption text-fg-subtle hover:text-fg" onClick={onClear}>
              Clear
            </button>
          ) : null}
        </div>
        {recent.length ? (
          <ul className="flex flex-col">
            {recent.map((q) => (
              <li key={q}>
                <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-body text-fg hover:bg-subtle" onClick={() => onPick(q)}>
                  <Clock className="size-3.5 text-fg-subtle" aria-hidden="true" />
                  <span className="truncate">{q}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-caption text-fg-subtle">Searches you run appear here, on this browser only.</p>
        )}
      </section>
      <section>
        <PanelLabel className="mb-3 block">Try</PanelLabel>
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((q) => (
            <button key={q} className={chip(false)} onClick={() => onPick(q)}>
              {q}
            </button>
          ))}
        </div>
        <p className="mt-4 text-caption text-fg-subtle">
          Search matches by meaning and by exact words, then shows only what you&apos;re allowed to see.
        </p>
      </section>
    </div>
  );
}

function ResultsSkeleton() {
  // Shown only after 150 ms, so fast searches never flash a skeleton.
  const [show, setShow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShow(true), 150);
    return () => clearTimeout(t);
  }, []);
  if (!show) return null;
  return (
    <div className="flex flex-col gap-1 px-6 pt-6" aria-busy="true" aria-label="Searching">
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div key={i} className="px-4 py-3">
          <Skeleton className="h-3 w-48" />
          <Skeleton className="mt-2 h-4 w-72" />
          <Skeleton className="mt-2 h-3 w-full" />
        </div>
      ))}
    </div>
  );
}
