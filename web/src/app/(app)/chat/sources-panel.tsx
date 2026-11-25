"use client";

import { ArrowLeft, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { PanelLabel } from "@/components/panel";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiProblem } from "@/lib/api/client";
import { useDocument } from "@/lib/api/hooks";
import type { SourceOut } from "@/lib/chat";
import { cn } from "@/lib/utils";

/**
 * The sources of one answer, and a document preview (UI_DESIGN §4.3). Previews always go
 * through the ACL-checked documents endpoint; a 404 shows one message for "missing" and
 * "not yours", so access isn't leaked.
 */
export function SourcesPanel({
  sources,
  cited,
  selected,
  onSelect,
  onClose,
}: {
  sources: SourceOut[];
  cited: number[];
  selected: number | null;
  onSelect: (n: number | null) => void;
  onClose: () => void;
}) {
  const source = sources.find((s) => s.n === selected) ?? null;
  return (
    <aside
      aria-label="Sources"
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[400px] flex-col border-l border-border bg-bg shadow-lg min-[1280px]:sticky min-[1280px]:top-0 min-[1280px]:h-svh min-[1280px]:shadow-none"
    >
      <div className="flex h-14 shrink-0 items-center gap-2 border-b border-border px-4">
        {source ? (
          <Button variant="ghost" size="icon-sm" aria-label="Back to sources" onClick={() => onSelect(null)}>
            <ArrowLeft />
          </Button>
        ) : null}
        <h2 className="flex-1 text-ui font-semibold text-fg">
          Sources <span className="ml-1 rounded-full bg-muted px-1.5 text-data text-fg-muted">{sources.length}</span>
        </h2>
        <Button variant="ghost" size="icon-sm" aria-label="Close sources" onClick={onClose}>
          <X />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {source ? (
          <Preview source={source} />
        ) : (
          <ul className="divide-y divide-border">
            {sources.map((s) => (
              <li key={s.n}>
                <button type="button" onClick={() => onSelect(s.n)} className="flex w-full gap-3 px-4 py-3 text-left hover:bg-subtle">
                  <span
                    className={cn(
                      "inline-flex size-[22px] shrink-0 items-center justify-center rounded-xs text-[12px] font-semibold tabular-nums",
                      cited.includes(s.n) ? "bg-brand text-white" : "bg-muted text-fg-muted",
                    )}
                  >
                    {s.n}
                  </span>
                  <span className="min-w-0">
                    <span className="line-clamp-2 text-caption font-medium text-fg">{s.title}</span>
                    {s.heading_path ? <span className="block truncate text-data text-fg-subtle">{s.heading_path}</span> : null}
                    {s.snippet ? <span className="mt-1 line-clamp-3 text-data text-fg-body">{s.snippet}</span> : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

function Preview({ source }: { source: SourceOut }) {
  const doc = useDocument(source.document_id);
  const focus = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    focus.current?.scrollIntoView?.({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [doc.data, source.ordinal]);

  if (doc.isPending) {
    return (
      <div className="flex flex-col gap-2 p-4" aria-busy="true">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-5/6" />
      </div>
    );
  }
  if (doc.isError) {
    const gone = doc.error instanceof ApiProblem && (doc.error.status === 404 || doc.error.status === 403);
    return <p className="p-4 text-caption text-fg-muted">{gone ? "This document isn't available." : "Couldn't load this document."}</p>;
  }
  return (
    <div className="p-4">
      <h3 className="text-[15px] font-semibold text-fg">{doc.data.title}</h3>
      <p className="mb-4 text-data text-fg-subtle">{doc.data.source_name}</p>
      {doc.data.chunks.map((c) => {
        const hit = c.ordinal === source.ordinal;
        return (
          <div
            key={c.ordinal}
            ref={hit ? focus : undefined}
            className={cn(
              "mb-3 rounded-r-md py-2 pr-3 pl-3 text-body whitespace-pre-wrap text-fg-body",
              hit && "border-l-2 border-brand bg-brand-subtle",
            )}
          >
            {c.heading_path ? <PanelLabel className="mb-1 block">{c.heading_path}</PanelLabel> : null}
            {c.content}
          </div>
        );
      })}
    </div>
  );
}
