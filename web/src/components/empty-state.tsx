import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

// Empty states per docs/UI_DESIGN.md §3.8: icon tile, title, one or two sentences, one action.
export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon: LucideIcon;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="mx-auto flex max-w-[400px] flex-col items-center px-4 py-16 text-center">
      <span className="mb-4 inline-flex size-10 items-center justify-center rounded-md border border-border">
        <Icon className="size-5 text-fg-muted" strokeWidth={1.6} aria-hidden="true" />
      </span>
      <h2 className="text-base font-medium text-fg">{title}</h2>
      <p className="mt-1 text-sm text-fg-subtle">{children}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}
