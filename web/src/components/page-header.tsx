import type { ReactNode } from "react";

export function PageHeader({ title, actions, children }: { title: string; actions?: ReactNode; children?: ReactNode }) {
  return (
    <header className="px-4 pt-8 pb-6 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-[22px] leading-7 font-medium tracking-[-0.03em] text-fg">{title}</h1>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}
