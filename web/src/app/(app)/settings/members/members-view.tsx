"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Copy, LogOut, Mail, Trash2, UserPlus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { resetWorkspaceState } from "@/lib/workspace-reset";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys, useInvites, useMe, useMembers, useWorkspaces, type Member } from "@/lib/api/hooks";
import { relativeTime } from "@/lib/format";

type InvitableRole = "admin" | "member" | "viewer";
const ROLES: { value: InvitableRole; label: string; hint: string }[] = [
  { value: "member", label: "Member", hint: "Searches shared sources and connects their own" },
  { value: "viewer", label: "Viewer", hint: "Searches and chats over shared sources only" },
  { value: "admin", label: "Admin", hint: "Also invites and removes members" },
];

/**
 * Settings › Members (UI_DESIGN §4.6, ADR-0030): who is in this workspace, invitations, roles.
 * Members only ever see sources someone chose to share; nothing here widens access.
 */
export function MembersView() {
  const me = useMe();
  const members = useMembers();
  const spaces = useWorkspaces();
  const role = me.data?.role;
  const isAdmin = role === "owner" || role === "admin";
  const invites = useInvites(isAdmin);
  const client = useQueryClient();
  const [leaving, setLeaving] = useState<Member | null>(null);
  const workspace = spaces.data?.find((w) => w.active);

  const refresh = () => {
    void client.invalidateQueries({ queryKey: keys.members });
    void client.invalidateQueries({ queryKey: keys.invites });
    void client.invalidateQueries({ queryKey: keys.workspaces });
  };

  async function changeRole(m: Member, next: InvitableRole) {
    try {
      const r = await api.PATCH("/api/v1/workspaces/members/{user_id}", { params: { path: { user_id: m.user_id } }, body: { role: next } });
      if (!r.response.ok) unwrap(r);
      toast.success(`${m.name || m.email} is now ${next}`);
      refresh();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  return (
    <>
      <PageHeader title="Members" description={workspace ? `People in ${workspace.name}` : undefined} />
      <div className="mx-auto flex w-full max-w-[800px] flex-col gap-8 px-4 py-6 sm:px-8">
        {isAdmin ? <InviteForm canInviteAdmin={role === "owner"} onInvited={refresh} /> : null}

        <section aria-labelledby="members-h">
          <h2 id="members-h" className="mb-2 text-ui font-semibold text-fg">
            Members
          </h2>
          {members.isPending ? (
            <Skeleton className="h-24 w-full" />
          ) : members.isError ? (
            <p role="alert" className="text-caption text-danger">
              {errorMessage(members.error)}
            </p>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border">
              {members.data.map((m) => (
                <li key={m.user_id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <span className="inline-flex size-8 items-center justify-center rounded-full bg-subtle text-caption font-semibold text-fg-muted" aria-hidden="true">
                    {(m.name || m.email).slice(0, 1).toUpperCase()}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body font-medium text-fg">
                      {m.name || m.email} {m.you ? <span className="text-fg-subtle">(you)</span> : null}
                    </span>
                    <span className="block truncate text-caption text-fg-subtle">
                      {m.email} · joined {relativeTime(m.joined_at)}
                    </span>
                  </span>
                  {role === "owner" && m.role !== "owner" && !m.you ? (
                    <select
                      aria-label={`Role for ${m.email}`}
                      value={m.role}
                      onChange={(e) => void changeRole(m, e.target.value as InvitableRole)}
                      className="h-8 rounded-md border border-border-input bg-bg px-2 text-caption"
                    >
                      {ROLES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="text-caption text-fg-muted capitalize" title={m.you && m.role !== "owner" ? undefined : "You can't change your own role"}>
                      {m.role}
                    </span>
                  )}
                  {m.role !== "owner" && (m.you || (isAdmin && (role === "owner" || m.role !== "admin"))) ? (
                    <Button variant="ghost" size="sm" onClick={() => setLeaving(m)} aria-label={m.you ? "Leave workspace" : `Remove ${m.email}`}>
                      {m.you ? <LogOut aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
                      {m.you ? "Leave" : "Remove"}
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>

        {isAdmin ? (
          <section aria-labelledby="invites-h">
            <h2 id="invites-h" className="mb-2 text-ui font-semibold text-fg">
              Pending invitations
            </h2>
            {!invites.data?.length ? (
              <p className="text-caption text-fg-subtle">No open invitations.</p>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border">
                {invites.data.map((i) => (
                  <li key={i.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                    <Mail className="size-4 text-fg-subtle" aria-hidden="true" />
                    <span className="min-w-0 flex-1 truncate text-body text-fg">{i.email}</span>
                    <span className="text-caption text-fg-muted capitalize">{i.role}</span>
                    <span className="text-caption text-fg-subtle">expires {relativeTime(i.expires_at)}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={async () => {
                        const r = await api.DELETE("/api/v1/workspaces/invites/{invite_id}", { params: { path: { invite_id: i.id } } });
                        if (r.response.ok) toast.success("Invitation revoked");
                        else toast.error("Couldn't revoke the invitation");
                        refresh();
                      }}
                    >
                      Revoke
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}
      </div>

      <RemoveDialog member={leaving} onClose={() => setLeaving(null)} onDone={refresh} />
    </>
  );
}

function InviteForm({ canInviteAdmin, onInvited }: { canInviteAdmin: boolean; onInvited: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InvitableRole>("member");
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<string | null>(null);

  return (
    <section aria-labelledby="invite-h" className="rounded-lg border border-border p-4">
      <h2 id="invite-h" className="mb-1 text-ui font-semibold text-fg">
        Invite someone
      </h2>
      <p className="mb-3 text-caption text-fg-muted">
        They&apos;ll see only sources members choose to share. Everything they connect stays private to them unless they share it.
      </p>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setLink(null);
          try {
            const done = unwrap(await api.POST("/api/v1/workspaces/invites", { body: { email: email.trim(), role } }));
            if (done.emailed) toast.success(`Invitation sent to ${email.trim()}`);
            else setLink(done.link.startsWith("/") ? `${window.location.origin}${done.link}` : done.link);
            setEmail("");
            onInvited();
          } catch (err) {
            toast.error(errorMessage(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="flex min-w-[220px] flex-1 flex-col gap-1 text-caption font-medium text-fg">
          Email
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="colleague@company.com"
            className="h-9 rounded-md border border-border-input bg-bg px-3 text-body font-normal"
          />
        </label>
        <label className="flex flex-col gap-1 text-caption font-medium text-fg">
          Role
          <select value={role} onChange={(e) => setRole(e.target.value as InvitableRole)} className="h-9 rounded-md border border-border-input bg-bg px-2 text-body font-normal">
            {ROLES.filter((r) => canInviteAdmin || r.value !== "admin").map((r) => (
              <option key={r.value} value={r.value} title={r.hint}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" variant="primary" loading={busy} disabled={!email.trim()}>
          <UserPlus aria-hidden="true" />
          Invite
        </Button>
      </form>
      {link ? (
        <div role="status" className="mt-3 rounded-md border border-border bg-subtle px-3 py-2 text-caption text-fg-muted">
          Email isn&apos;t set up here, so share this link with them (it only works for that address):
          <span className="mt-1 flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate font-mono text-data text-fg">{link}</code>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void navigator.clipboard.writeText(link);
                toast.success("Link copied");
              }}
            >
              <Copy aria-hidden="true" />
              Copy
            </Button>
          </span>
        </div>
      ) : null}
    </section>
  );
}

function RemoveDialog({ member, onClose, onDone }: { member: Member | null; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();
  const router = useRouter();
  const self = member?.you ?? false;
  return (
    <Dialog open={member !== null} onOpenChange={(o) => (!o ? onClose() : null)}>
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>{self ? "Leave this workspace?" : `Remove ${member?.name || member?.email}?`}</DialogTitle>
          <DialogDescription>
            {self
              ? "Everything you connected or uploaded here is deleted from Slate, including sources you shared. Your own workspace isn't affected."
              : "Everything they connected or uploaded here is deleted from Slate, including sources they shared with the workspace."}
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={busy}
            onClick={async () => {
              if (!member) return;
              setBusy(true);
              try {
                const r = unwrap(await api.DELETE("/api/v1/workspaces/members/{user_id}", { params: { path: { user_id: member.user_id } } }));
                toast.success(self ? "You left the workspace" : `Removed. ${r.sources_deleted} source(s) deleted.`);
                if (self) {
                  resetWorkspaceState(client); // that workspace's data must not linger in the cache
                  router.push("/home");
                }
                else {
                  onDone();
                  onClose();
                }
              } catch (err) {
                toast.error(errorMessage(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            {self ? "Leave" : "Remove"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
