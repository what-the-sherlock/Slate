"use client";

import { ArrowUpRight } from "lucide-react";
import type { ReactNode } from "react";
import { ConnectorIcon } from "@/components/brand";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { useDocument } from "@/lib/api/hooks";
import { safeUrl, type SourceOut } from "@/lib/chat";
import { relativeTime } from "@/lib/format";

/**
 * The citation hover card (UI_DESIGN §3.10, P2-2): source, title, location, the cited
 * passage and a footer with the date, the author and "Open source ↗".
 *
 * The passage comes from the answer's own `sources` event, which the server already
 * ACL-checked. The external link is never built here: it comes from the ACL-checked
 * documents endpoint, fetched only once the card opens, and only http(s) links are
 * shown (an upload has none).
 */
export function CitationCard({ source, children }: { source: SourceOut; children: ReactNode }) {
  return (
    <HoverCard>
      <HoverCardTrigger asChild>{children}</HoverCardTrigger>
      <HoverCardContent aria-label={`Preview of source ${source.n}`}>
        <CardBody source={source} />
      </HoverCardContent>
    </HoverCard>
  );
}

function CardBody({ source }: { source: SourceOut }) {
  const doc = useDocument(source.document_id); // mounted only while open: fetched lazily
  const href = doc.data ? safeUrl(doc.data.uri) : "";
  const when = source.updated_at ?? doc.data?.source_updated_at ?? null;
  return (
    <div className="flex flex-col">
      <div className="flex items-start gap-2 px-3.5 pt-3">
        <span className="mt-0.5 inline-flex size-4 shrink-0 items-center justify-center">
          <ConnectorIcon connector={source.connector ?? "upload"} className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-ui font-medium text-fg">{source.title}</p>
          {source.heading_path ? <p className="truncate text-caption text-fg-subtle">{source.heading_path}</p> : null}
        </div>
      </div>
      <blockquote className="mx-3.5 mt-2 line-clamp-5 border-l-2 border-brand bg-brand-subtle px-2.5 py-1.5 text-body leading-5 text-fg-body">
        {source.snippet}
      </blockquote>
      <div className="flex items-center gap-2 px-3.5 py-2.5 text-caption text-fg-subtle">
        <span className="min-w-0 flex-1 truncate">
          {when ? `Updated ${relativeTime(when)}` : null}
          {when && source.author ? " · " : null}
          {source.author ?? null}
        </span>
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="inline-flex shrink-0 items-center gap-0.5 font-medium text-brand-strong hover:underline"
          >
            Open source <ArrowUpRight className="size-3.5" aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </div>
  );
}
