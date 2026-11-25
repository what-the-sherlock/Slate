"use client";

import { Check, Copy } from "lucide-react";
import { memo, useMemo, useState, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { citationMarkdown, safeUrl, type SourceOut } from "@/lib/chat";
import { repairMarkdown, splitBlocks } from "@/lib/markdown-stream";
import { cn } from "@/lib/utils";

/**
 * Answer Markdown (UI_DESIGN §3.11), safe for untrusted model output:
 * - no raw HTML (`skipHtml`), only http(s) links (`safeUrl`), shown with their host,
 *   since a planted document could have written them;
 * - no images: an image URL is fetched on render, so a planted ![](https://x/?d=…) could
 *   leak other sources' text without a click (security review);
 * - a `[n]` is a citation badge only once the server confirmed it; while streaming, a
 *   marker for a shown source is a muted placeholder of the same size (no reflow).
 *
 * While streaming, the text is repaired (open fences, half-written links) and split into
 * blocks; finished blocks are memoised, so each token re-renders only the last one.
 */
export function AnswerMarkdown({
  text,
  cited,
  sources,
  streaming,
  onCite,
}: {
  text: string;
  cited: number[];
  sources: SourceOut[];
  streaming: boolean;
  onCite: (n: number) => void;
}) {
  const shown = streaming ? repairMarkdown(text) : text;
  const pending = useMemo(() => (streaming ? sources.map((s) => s.n) : []), [streaming, sources]);
  const blocks = useMemo(() => splitBlocks(citationMarkdown(shown, cited, pending)), [shown, cited, pending]);
  const titles = useMemo(() => new Map(sources.map((s) => [s.n, s.title])), [sources]);
  return (
    <>
      {blocks.map((block, i) => (
        <Block key={i} markdown={block} titles={titles} onCite={onCite} />
      ))}
    </>
  );
}

const Block = memo(function Block({
  markdown,
  titles,
  onCite,
}: {
  markdown: string;
  titles: Map<number, string>;
  onCite: (n: number) => void;
}) {
  const components = useMemo(() => buildComponents(titles, onCite), [titles, onCite]);
  return (
    <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={components}>
      {markdown}
    </Markdown>
  );
});

function buildComponents(titles: Map<number, string>, onCite: (n: number) => void): Components {
  return {
    a({ href, children }) {
      const cite = href?.match(/^cite(-pending)?:(\d+)$/);
      if (cite) {
        const n = Number(cite[2]);
        if (cite[1]) {
          return (
            <span
              aria-hidden="true"
              className="relative -top-px mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-xs bg-muted px-1 align-middle text-[11px] font-semibold text-fg-subtle tabular-nums"
            >
              {n}
            </span>
          );
        }
        return (
          <button
            type="button"
            onClick={() => onCite(n)}
            aria-label={`Source ${n}: ${titles.get(n) ?? "document"}`}
            className="sl-pop relative -top-px mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-xs bg-brand px-1 align-middle text-[11px] font-semibold text-white tabular-nums transition-colors hover:bg-brand-strong motion-reduce:animate-none"
          >
            {n}
          </button>
        );
      }
      if (!href) return <span>{children}</span>;
      let host = "";
      try {
        host = new URL(href).hostname;
      } catch {
        return <span>{children}</span>;
      }
      return (
        <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-brand underline decoration-brand/40 underline-offset-2 hover:decoration-brand">
          {children}
          <span className="ml-1 text-data text-fg-subtle no-underline">({host})</span>
        </a>
      );
    },
    img({ alt }) {
      return alt ? <span className="text-fg-subtle">[image: {alt}]</span> : null;
    },
    code({ children, className }) {
      return /language-/.test(className ?? "") ? (
        <code className="font-mono">{children}</code>
      ) : (
        <code className="rounded-xs bg-muted px-1 py-px font-mono text-[0.88em] text-fg">{children}</code>
      );
    },
    pre({ children }) {
      return <CodeBlock>{children}</CodeBlock>;
    },
    table({ children }) {
      return (
        <div className="overflow-x-auto rounded-md border border-border [&_tr:last-child_td]:border-b-0">
          <table>{children}</table>
        </div>
      );
    },
  };
}

/** Fenced code (UI_DESIGN §3.11): a header with the language and a copy button. */
function CodeBlock({ children }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const child = Array.isArray(children) ? children[0] : children;
  const props = (child as { props?: { className?: string; children?: ReactNode } } | null)?.props;
  const language = props?.className?.match(/language-([\w+#-]+)/)?.[1] ?? "text";
  const code = String(props?.children ?? "").replace(/\n$/, "");
  return (
    <div className="overflow-hidden rounded-md border border-border bg-subtle">
      <div className="flex h-9 items-center justify-between border-b border-border pr-1.5 pl-3">
        <span className="font-mono text-data text-fg-subtle">{language}</span>
        <button
          type="button"
          onClick={() =>
            void navigator.clipboard?.writeText(code).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            })
          }
          className="inline-flex h-6 items-center gap-1 rounded-sm px-1.5 text-data text-fg-subtle transition-colors hover:bg-muted hover:text-fg"
          aria-label={copied ? "Copied" : "Copy code"}
        >
          {copied ? <Check className="size-3.5 animate-in zoom-in-50 duration-150" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className={cn("max-h-[400px] overflow-auto px-4 py-3 font-mono text-[13px] leading-5 text-fg")}>{children}</pre>
    </div>
  );
}
