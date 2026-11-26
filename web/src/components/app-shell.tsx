"use client";

import { cn } from "@/lib/utils";
import { Activity, ChevronsLeft, House, LogOut, Menu, MessageSquare, Plug, Search, SquarePen, Users, X, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { Logo } from "@/components/brand";
import { Button, Spinner } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";
import { useMe, useSignOut, type Me } from "@/lib/api/hooks";

type NavItem = { href: string; label: string; icon: LucideIcon };

const NAV: readonly NavItem[] = [
  { href: "/home", label: "Home", icon: House },
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/search", label: "Search", icon: Search },
  { href: "/sources", label: "Sources", icon: Plug },
  { href: "/settings/members", label: "Members", icon: Users },
];

const COLLAPSED_KEY = "slate.sidebar.collapsed";
// Chat has its own history column, so the nav defaults to the icon rail there (a separate
// preference: collapsing it in Chat doesn't collapse it everywhere).
const CHAT_COLLAPSED_KEY = "slate.sidebar.collapsed.chat";

/**
 * The rail state lives in localStorage, an external store, so it is read with
 * useSyncExternalStore. The server snapshot is "expanded", so SSR and hydration agree.
 */
const collapsedListeners = new Set<() => void>();
function subscribeCollapsed(cb: () => void) {
  collapsedListeners.add(cb);
  window.addEventListener("storage", cb);
  return () => {
    collapsedListeners.delete(cb);
    window.removeEventListener("storage", cb);
  };
}
function useCollapsed(inChat: boolean): [boolean, (next: boolean) => void] {
  const key = inChat ? CHAT_COLLAPSED_KEY : COLLAPSED_KEY;
  const collapsed = useSyncExternalStore(
    subscribeCollapsed,
    () => {
      const stored = window.localStorage.getItem(key);
      return stored === null ? inChat : stored === "1";
    },
    () => inChat,
  );
  return [
    collapsed,
    (next) => {
      window.localStorage.setItem(key, next ? "1" : "0");
      collapsedListeners.forEach((cb) => cb());
    },
  ];
}

/** Signed-in shell (UI_DESIGN §3.9, §4.2): collapsible sidebar + main, auth and verification guard. */
export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const [drawer, setDrawer] = useState(false);
  const [collapsed, setCollapsed] = useCollapsed(pathname === "/chat");

  useEffect(() => {
    if (me.data === null) router.replace(`/sign-in?next=${encodeURIComponent(pathname)}`);
    // Unverified accounts can't use the app yet (ADR-0021); the API refuses them too.
    else if (me.data && !me.data.email_verified) router.replace("/verify");
  }, [me.data, router, pathname]);

  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  if (!me.data || !me.data.email_verified) {
    return (
      <div className="flex flex-1 items-center justify-center" aria-busy="true">
        <Spinner className="size-5 text-fg-subtle" />
        <span className="sr-only">Loading</span>
      </div>
    );
  }

  return (
    <div className="flex min-h-svh bg-page">
      <aside
        className={cn(
          "sticky top-0 hidden h-svh shrink-0 border-r border-border bg-bg transition-[width] duration-200 ease-[var(--sl-ease-out)] motion-reduce:transition-none lg:block",
          collapsed ? "w-[60px]" : "w-[200px]",
        )}
      >
        <SidebarContent me={me.data} collapsed={collapsed} onCollapsedChange={setCollapsed} />
      </aside>

      {drawer ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button className="absolute inset-0 bg-[var(--sl-overlay)]" aria-label="Close navigation" onClick={() => setDrawer(false)} />
          <aside className="absolute inset-y-0 left-0 w-[260px] border-r border-border bg-bg shadow-lg">
            <SidebarContent me={me.data} collapsed={false} onNavigate={() => setDrawer(false)} />
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Close navigation"
              className="absolute top-3.5 right-3"
              onClick={() => setDrawer(false)}
            >
              <X />
            </Button>
          </aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-bg px-3 lg:hidden">
          <Button variant="ghost" size="icon-sm" aria-label="Open navigation" onClick={() => setDrawer(true)}>
            <Menu />
          </Button>
          <Logo />
        </div>

        <main id="main" className="flex-1">
          {children}
        </main>
      </div>
    </div>
  );
}

function SidebarContent({
  me,
  collapsed,
  onCollapsedChange,
  onNavigate,
}: {
  me: Me;
  collapsed: boolean;
  onCollapsedChange?: (next: boolean) => void;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`);

  return (
    <nav className="flex h-full flex-col" aria-label="Main">
      {/* h-14 matches the page bar, so the two hairlines line up. */}
      <div className={cn("flex h-14 shrink-0 items-center border-b border-border", collapsed ? "justify-center px-2" : "px-4")}>
        <Link href="/home" onClick={onNavigate} aria-label="Slate home" className="rounded-md">
          <Logo withWordmark={!collapsed} />
        </Link>
      </div>

      <div className="flex-1 space-y-0.5 overflow-y-auto px-2 py-3">
        <div className="mb-2">
          <WorkspaceSwitcher collapsed={collapsed} />
        </div>
        <NavLink href="/chat" label="New chat" icon={SquarePen} collapsed={collapsed} onNavigate={onNavigate} emphasis />
        <div className="-mx-2 my-3 h-px bg-border" />
        {NAV.map((item) => (
          <NavLink key={item.href} {...item} active={isActive(item.href)} collapsed={collapsed} onNavigate={onNavigate} />
        ))}
      </div>

      <div className="shrink-0 space-y-0.5 border-t border-border px-2 py-3">
        <NavLink href="/status" label="System status" icon={Activity} active={isActive("/status")} collapsed={collapsed} onNavigate={onNavigate} />
        <Account me={me} collapsed={collapsed} />
        {onCollapsedChange ? (
          <button
            type="button"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => onCollapsedChange(!collapsed)}
            className="flex w-full items-center justify-center rounded-sm px-2 py-1.5 text-fg-subtle transition-colors hover:bg-muted hover:text-fg-muted"
          >
            <ChevronsLeft
              className={cn("size-4 transition-transform duration-300 motion-reduce:transition-none", collapsed && "rotate-180")}
              aria-hidden="true"
            />
          </button>
        ) : null}
      </div>
    </nav>
  );
}

function NavLink({
  href,
  label,
  icon: Icon,
  active = false,
  collapsed,
  emphasis = false,
  onNavigate,
}: NavItem & { active?: boolean; collapsed: boolean; emphasis?: boolean; onNavigate?: () => void }) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      aria-label={collapsed ? label : undefined}
      title={collapsed ? label : undefined}
      className={cn(
        "flex w-full items-center rounded-sm text-caption transition-colors duration-150",
        collapsed ? "justify-center px-2 py-2" : "gap-2.5 px-2 py-1.5",
        active
          ? "bg-brand-subtle font-medium text-brand"
          : emphasis
            ? "font-medium text-fg hover:bg-muted"
            : "text-fg-muted hover:bg-muted hover:text-fg",
      )}
    >
      <Icon className="size-[15px] shrink-0" strokeWidth={1.75} aria-hidden="true" />
      {collapsed ? null : <span className="truncate">{label}</span>}
    </Link>
  );
}

function Account({ me, collapsed }: { me: Me; collapsed: boolean }) {
  const router = useRouter();
  const signOut = useSignOut();
  const initials = me.user.name
    .split(/\s+/)
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const avatar = (
    <span
      className={cn(
        "inline-flex size-5 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold",
        "bg-brand-subtle text-brand",
      )}
      aria-hidden="true"
    >
      {initials}
    </span>
  );
  const button = (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label="Sign out"
      loading={signOut.isPending}
      onClick={() => signOut.mutate(undefined, { onSettled: () => router.replace("/sign-in") })}
    >
      {signOut.isPending ? null : <LogOut />}
    </Button>
  );

  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-1 py-1">
        <span title={me.user.name}>{avatar}</span>
        {button}
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2.5 rounded-sm py-1 pr-0.5 pl-2">
      {avatar}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-caption font-medium text-fg">{me.user.name}</span>
        <span className="block text-data text-fg-subtle capitalize">{me.role}</span>
      </span>
      <Tooltip>
        <TooltipTrigger asChild>{button}</TooltipTrigger>
        <TooltipContent>Sign out</TooltipContent>
      </Tooltip>
    </div>
  );
}
