"use client";

import { cn } from "@/lib/utils";
import { ArrowDown, ArrowUp, Ban, RotateCcw, Search, Trash2, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { EmptyState } from "@/components/empty-state";
import { Pill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiProblem, errorMessage } from "@/lib/api/client";
import { useAdminUsers, useDeleteUser, useSetUserDisabled, type AdminUser, type AdminUserFilters, type AdminUserSort, type AdminUserStatus } from "@/lib/api/hooks";
import { absoluteTime, formatCount, relativeTime } from "@/lib/format";
import { accountStatus } from "./labels";

const STATUSES: { value: AdminUserStatus; label: string }[] = [
  { value: "all", label: "All accounts" },
  { value: "active", label: "Active (30 days)" },
  { value: "inactive", label: "Inactive (30 days)" },
  { value: "verified", label: "Verified" },
  { value: "unverified", label: "Unverified" },
  { value: "disabled", label: "Disabled" },
];

const TH = "px-4 text-label font-semibold tracking-wider text-fg-subtle uppercase first:pl-4 last:pr-4 sm:first:pl-8 sm:last:pr-8";
const TD = "px-4 first:pl-4 last:pr-4 sm:first:pl-8 sm:last:pr-8";

/** Admin › Users: search, filter and sort accounts; disable or re-enable one. */
export function UsersPanel() {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<AdminUserStatus>("all");
  const [sort, setSort] = useState<AdminUserSort>("joined");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [target, setTarget] = useState<AdminUser | null>(null);
  const [deleting, setDeleting] = useState<AdminUser | null>(null);

  // Debounced: one request after typing pauses, not one per keystroke (Neon CU budget).
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 250);
    return () => clearTimeout(t);
  }, [text]);

  const filters: AdminUserFilters = { q, status, sort, order };
  const users = useAdminUsers(filters);
  const rows = users.data?.pages.flatMap((p) => p.items) ?? [];
  const total = users.data?.pages[0]?.total;
  const filtered = q !== "" || status !== "all";

  const sortBy = (next: AdminUserSort) => {
    if (next === sort) setOrder(order === "desc" ? "asc" : "desc");
    else {
      setSort(next);
      setOrder(next === "email" ? "asc" : "desc");
    }
  };

  return (
    <>
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3 sm:px-8">
        <label className="relative block w-full sm:w-72">
          <span className="sr-only">Search by name or email</span>
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
          <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Search by name or email" maxLength={100} className="h-9 pl-8" />
        </label>
        <label className="flex items-center gap-2 text-caption text-fg-muted">
          Show
          <select value={status} onChange={(e) => setStatus(e.target.value as AdminUserStatus)} className="h-9 rounded-lg border border-input bg-bg px-2 text-caption text-fg outline-none focus-visible:ring-2 focus-visible:ring-brand">
            {STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <span className="ml-auto text-caption text-fg-subtle tabular-nums" aria-live="polite">
          {total === undefined ? " " : `${formatCount(total)} ${total === 1 ? "account" : "accounts"}`}
        </span>
      </div>

      {users.isError ? (
        <p role="alert" className="mx-4 mt-6 rounded-md border border-danger-border bg-danger-subtle px-3 py-2.5 text-caption text-danger sm:mx-8">
          {errorMessage(users.error)}{" "}
          <button className="font-medium underline" onClick={() => users.refetch()}>
            Retry
          </button>
        </p>
      ) : users.isPending ? (
        <TableSkeleton />
      ) : rows.length === 0 ? (
        <EmptyState icon={Users} title={filtered ? "No accounts match" : "No accounts yet"}>
          {filtered ? "Try a different search or show all accounts." : "Accounts appear here as people sign up."}
        </EmptyState>
      ) : (
        <div className={cn("overflow-x-auto transition-opacity", users.isPlaceholderData && "opacity-60")} aria-busy={users.isFetching}>
          <table className="w-full min-w-[760px] border-collapse text-caption">
            <thead>
              <tr className="h-9 border-b border-border bg-subtle text-left">
                <SortHeader label="Account" active={sort === "email"} order={order} onClick={() => sortBy("email")} />
                <th scope="col" className={TH}>Status</th>
                <SortHeader label="Joined" active={sort === "joined"} order={order} onClick={() => sortBy("joined")} />
                <SortHeader label="Last active" active={sort === "last_active"} order={order} onClick={() => sortBy("last_active")} />
                <th scope="col" className={`${TH} text-right`}>Workspaces</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((u) => {
                const s = accountStatus(u);
                return (
                  <tr key={u.id} className="h-14 border-b border-border transition-colors hover:bg-subtle">
                    <td className={cn(TD, "max-w-[320px]")}>
                      <span className="block truncate text-body font-medium text-fg" title={u.name}>
                        {u.name || u.email} {u.you ? <span className="font-normal text-fg-subtle">(you)</span> : null}
                      </span>
                      <span className="block truncate text-data text-fg-subtle" title={u.email}>
                        {u.email}
                      </span>
                    </td>
                    <td className={TD}>
                      <Pill variant={s.variant}>{s.label}</Pill>
                    </td>
                    <td className={`${TD} text-fg-muted`}>
                      <time dateTime={u.joined_at} title={absoluteTime(u.joined_at)}>
                        {relativeTime(u.joined_at)}
                      </time>
                    </td>
                    <td className={`${TD} text-fg-muted`}>
                      {u.last_active_at ? (
                        <time dateTime={u.last_active_at} title={absoluteTime(u.last_active_at)}>
                          {relativeTime(u.last_active_at)}
                        </time>
                      ) : (
                        <span className="text-fg-subtle">Never</span>
                      )}
                    </td>
                    <td className={`${TD} text-right text-fg tabular-nums`}>{formatCount(u.workspaces)}</td>
                    <td className={`${TD} text-right`}>
                      {u.you ? null : (
                        <span className="inline-flex items-center justify-end gap-1">
                          {/* Fixed width so Delete lines up whichever label is showing. */}
                          {u.disabled ? (
                            <Button variant="ghost" size="sm" onClick={() => setTarget(u)} aria-label={`Re-enable ${u.email}`} className="w-[104px] justify-start">
                              <RotateCcw aria-hidden="true" />
                              Re-enable
                            </Button>
                          ) : (
                            <Button variant="ghost" size="sm" onClick={() => setTarget(u)} aria-label={`Disable ${u.email}`} className="w-[104px] justify-start">
                              <Ban aria-hidden="true" />
                              Disable
                            </Button>
                          )}
                          {/* Red is kept for the one action that can't be undone. */}
                          <Button variant="ghost" size="sm" onClick={() => setDeleting(u)} aria-label={`Delete ${u.email}`} className="text-danger hover:bg-danger-subtle hover:text-danger">
                            <Trash2 aria-hidden="true" />
                            Delete
                          </Button>
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {users.hasNextPage ? (
            <div className="flex justify-center py-4">
              <Button variant="secondary" onClick={() => users.fetchNextPage()} loading={users.isFetchingNextPage}>
                Load more
              </Button>
            </div>
          ) : null}
        </div>
      )}

      <StatusDialog user={target} onClose={() => setTarget(null)} />
      <DeleteDialog key={deleting?.id ?? "none"} user={deleting} onClose={() => setDeleting(null)} />
    </>
  );
}

function SortHeader({ label, active, order, onClick }: { label: string; active: boolean; order: "asc" | "desc"; onClick: () => void }) {
  const Icon = order === "asc" ? ArrowUp : ArrowDown;
  return (
    <th scope="col" className={TH} aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={onClick} className={cn("inline-flex items-center gap-1 rounded-sm uppercase outline-none hover:text-fg focus-visible:ring-2 focus-visible:ring-brand", active && "text-fg")}>
        {label}
        <Icon className={cn("size-3", !active && "invisible")} aria-hidden="true" />
      </button>
    </th>
  );
}

function StatusDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const mutate = useSetUserDisabled();
  const disabling = !!user && !user.disabled;
  const who = user?.name || user?.email;
  return (
    <Dialog open={user !== null} onOpenChange={(o) => (!o && !mutate.isPending ? onClose() : null)}>
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>{disabling ? `Disable ${who}?` : `Re-enable ${who}?`}</DialogTitle>
          <DialogDescription>
            {disabling
              ? `${user?.email} is signed out everywhere straight away and can't sign in again until you re-enable the account. Nothing they've connected or uploaded is deleted.`
              : `${user?.email} can sign in again. They'll need to sign in fresh; old sessions stay ended.`}
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={mutate.isPending}>
            Cancel
          </Button>
          <Button
            variant={disabling ? "danger" : "primary"}
            loading={mutate.isPending}
            onClick={() => {
              if (!user) return;
              mutate.mutate(
                { id: user.id, disabled: disabling },
                {
                  onSuccess: () => {
                    toast.success(disabling ? `${user.email} is disabled` : `${user.email} can sign in again`);
                    onClose();
                  },
                  onError: (err) => toast.error(errorMessage(err)),
                },
              );
            }}
          >
            {disabling ? "Disable account" : "Re-enable account"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Deleting can't be undone, so the dialog says exactly what goes, offers Disable as the
 * reversible alternative, and enables the button only once the email is typed (the server
 * checks it again).
 */
function DeleteDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const remove = useDeleteUser();
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const matches = !!user && typed.trim().toLowerCase() === user.email.toLowerCase();
  const who = user?.name || user?.email;

  return (
    <Dialog open={user !== null} onOpenChange={(o) => (!o && !remove.isPending ? onClose() : null)}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>Delete {who}?</DialogTitle>
          <DialogDescription>
            This permanently deletes the account and everything in its own workspace: sources, uploaded files, documents and chats. Anything it connected in other people&apos;s workspaces is removed too. It can&apos;t be undone. The email address can be used to sign up again.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!user || !matches) return;
            setError(null);
            remove.mutate(
              { id: user.id, confirmEmail: typed.trim() },
              {
                onSuccess: () => {
                  toast.success(`${user.email} was deleted`);
                  onClose();
                },
                // A 409 (owns a shared workspace) is explained in place, not as a toast.
                onError: (err) => (err instanceof ApiProblem && err.status === 409 ? setError(errorMessage(err)) : toast.error(errorMessage(err))),
              },
            );
          }}
        >
          <label className="block space-y-1.5 text-caption font-medium text-fg">
            <span>
              Type <span className="font-mono text-data font-normal text-fg-body select-all">{user?.email}</span> to confirm
            </span>
            <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} autoFocus aria-invalid={!!error} className="h-9 font-normal" />
          </label>
          {error ? (
            <p role="alert" className="rounded-md border border-danger-border bg-danger-subtle px-3 py-2 text-caption text-danger">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose} disabled={remove.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" loading={remove.isPending} disabled={!matches}>
              Delete account
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-0" aria-busy="true">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex h-14 items-center gap-4 border-b border-border px-4 sm:px-8">
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="h-3 w-56" />
          </div>
          <Skeleton className="h-5 w-16" />
          <Skeleton className="h-3.5 w-20" />
        </div>
      ))}
    </div>
  );
}
