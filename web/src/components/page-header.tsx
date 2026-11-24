import type { ReactNode } from "react";

/**
 * The page bar (UI_DESIGN §4.2): 56px tall, full-bleed, closed by a hairline, so it lines
 * up with the sidebar's header rule. Title on the left, page actions on the right.
 */
export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="border-b border-border">
      <div className="flex min-h-14 flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-8">
        <div className="min-w-0">
          <h1 className="text-page font-semibold tracking-[-0.02em] text-fg">{title}</h1>
          {description ? <p className="text-caption text-fg-subtle">{description}</p> : null}
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}
