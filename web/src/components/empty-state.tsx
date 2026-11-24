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
    <div className="mx-auto flex max-w-[380px] flex-col items-center px-4 py-16 text-center">
      <span className="mb-4 inline-flex size-10 items-center justify-center rounded-lg border border-border bg-subtle shadow-xs">
        <Icon className="size-[18px] text-fg-muted" strokeWidth={1.5} aria-hidden="true" />
      </span>
      <h2 className="text-ui font-semibold text-fg">{title}</h2>
      <p className="mt-1 text-body text-fg-subtle">{children}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}
