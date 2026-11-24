"use client";

import { cn } from "cn";
import { Info, LogOut, Menu, MessageSquare, Plug, Search, SquarePen } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Logo } from "@/components/brand";
import { Button, Spinner } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useMe, useSignOut, type Me } from "@/lib/api/hooks";

const NAV = [
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/search", label: "Search", icon: Search },
  { href: "/sources", label: "Sources", icon: Plug },
  // Settings (members, usage) arrives in M7; no dead links until then (UI_DESIGN §3.9).
] as const;

/** Signed-in shell (UI_DESIGN §3.9, §4.2): sidebar + main, guest banner, auth guard. */
export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    if (me.data === null) router.replace(`/sign-in?next=${encodeURIComponent(pathname)}`);
  }, [me.data, router, pathname]);

  if (!me.data) {
    return (
      <div className="flex flex-1 items-center justify-center" aria-busy="true">
        <Spinner className="size-5 text-fg-subtle" />
        <span className="sr-only">Loading</span>
      </div>
    );
  }

  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 hidden h-svh w-[248px] shrink-0 border-r border-border bg-subtle md:block">
        <Sidebar me={me.data} />
      </aside>
      {drawer ? (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button className="absolute inset-0 bg-[var(--sl-overlay)]" aria-label="Close navigation" onClick={() => setDrawer(false)} />
          <aside className="absolute inset-y-0 left-0 w-[280px] border-r border-border bg-subtle shadow-lg">
            <Sidebar me={me.data} onNavigate={() => setDrawer(false)} />
          </aside>
        </div>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-14 items-center gap-2 border-b border-border px-4 md:hidden">
          <Button variant="ghost" size="icon-sm" aria-label="Open navigation" onClick={() => setDrawer(true)}>
            <Menu />
          </Button>
          <Logo />
        </div>
        {me.data.is_guest ? (
          <div role="status" className="flex h-10 items-center gap-2 border-b border-brand-border bg-brand-subtle px-4 text-[13px] font-medium text-brand-strong sm:px-8">
            <Info className="size-4 shrink-0" aria-hidden="true" />
            <span className="truncate">You&apos;re exploring as a guest. Answers use public sources only.</span>
            <Link href="/sign-in" className="ml-auto shrink-0 underline-offset-[3px] hover:underline">
              Sign in
            </Link>
          </div>
        ) : null}
        <main id="main" className="flex-1">
          {children}
        </main>
      </div>
    </div>
  );
}

function Sidebar({ me, onNavigate }: { me: Me; onNavigate?: () => void }) {
  const pathname = usePathname();
  const items = NAV;
  return (
    <nav className="flex h-full flex-col gap-4 p-3" aria-label="Main">
      <div className="flex items-center gap-2.5 px-2 pt-1">
        <Logo />
      </div>
      <Button asChild variant="secondary" size="md" className="w-full justify-start">
        <Link href="/chat" onClick={onNavigate}>
          <SquarePen className="size-4" aria-hidden="true" />
          New chat
        </Link>
      </Button>
      <GlidingList>
        {items.map(({ href, label, icon: Icon }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={href}>
              <Link
                href={href}
                onClick={onNavigate}
                aria-current={active ? "page" : undefined}
                data-glide
                className={cn(
                  "relative z-10 flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium transition-colors",
                  active ? "border border-border bg-bg text-fg" : "text-fg-muted hover:text-fg",
                )}
              >
                <Icon className="size-4" strokeWidth={1.6} aria-hidden="true" />
                {label}
              </Link>
            </li>
          );
        })}
      </GlidingList>
      <div className="mt-auto">
        <UserMenu me={me} />
      </div>
    </nav>
  );
}

/** One highlight that glides between items on hover/focus (Beautiful UI sidebar). */
function GlidingList({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLUListElement>(null);
  const [box, setBox] = useState<{ top: number; height: number } | null>(null);
  function track(target: EventTarget | null) {
    const el = target instanceof HTMLElement ? target.closest<HTMLElement>("[data-glide]") : null;
    if (!el || !ref.current) return;
    const list = ref.current.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    setBox({ top: rect.top - list.top, height: rect.height });
  }
  return (
    <ul
      ref={ref}
      className="relative flex flex-col gap-0.5"
      onMouseOver={(e) => track(e.target)}
      onFocus={(e) => track(e.target)}
      onMouseLeave={() => setBox(null)}
      onBlur={() => setBox(null)}
    >
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 rounded-md bg-muted transition-[transform,opacity,height] duration-200 ease-[var(--sl-ease-out)] motion-reduce:transition-none"
        style={{ transform: `translateY(${box?.top ?? 0}px)`, height: box?.height ?? 32, opacity: box ? 1 : 0 }}
      />
      {children}
    </ul>
  );
}

function UserMenu({ me }: { me: Me }) {
  const router = useRouter();
  const signOut = useSignOut();
  const initials = me.user.name
    .split(/\s+/)
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div className="flex items-center gap-2.5 rounded-md px-2 py-1.5">
      <span
        className={cn(
          "inline-flex size-7 shrink-0 items-center justify-center rounded-full text-[12px] font-semibold",
          me.is_guest ? "border border-dashed border-border-strong text-fg-muted" : "bg-emphasis text-fg-muted",
        )}
        aria-hidden="true"
      >
        {me.is_guest ? "G" : initials}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-fg">{me.user.name}</span>
        <span className="block text-[12px] text-fg-subtle capitalize">{me.is_guest ? "Guest" : me.role}</span>
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Sign out"
            loading={signOut.isPending}
            onClick={() => signOut.mutate(undefined, { onSettled: () => router.replace("/sign-in") })}
          >
            {signOut.isPending ? null : <LogOut />}
          </Button>
        </TooltipTrigger>
        <TooltipContent>Sign out</TooltipContent>
      </Tooltip>
    </div>
  );
}
