"use client";

import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiProblem, unwrap } from "./client";
import type { components } from "./schema";

export type Me = components["schemas"]["MeResponse"];
export type Source = components["schemas"]["SourceOut"];
export type Run = components["schemas"]["RunOut"];
export type JobState = components["schemas"]["JobOut"];
export type SearchBody = components["schemas"]["SearchBody"];
export type SearchOut = components["schemas"]["SearchOut"];
export type SearchHit = components["schemas"]["SearchHitOut"];
export type DocumentPreview = components["schemas"]["DocumentPreviewOut"];

export const keys = {
  me: ["me"] as const,
  authConfig: ["auth-config"] as const,
  sources: ["sources"] as const,
  runs: (id: string) => ["runs", id] as const,
  job: (id: string) => ["job", id] as const,
  search: (body: SearchBody | null) => ["search", body] as const,
  document: (id: string) => ["document", id] as const,
  chatSessions: ["chat-sessions"] as const,
  chatMessages: (id: string | null) => ["chat-messages", id] as const,
  workspaces: ["workspaces"] as const,
  members: ["members"] as const,
  invites: ["invites"] as const,
  activity: ["activity"] as const,
  integrations: ["integrations"] as const,
  admin: ["admin"] as const,
  adminOverview: ["admin", "overview"] as const,
  adminSignups: (days: AdminRange) => ["admin", "signups", days] as const,
  adminFailed: ["admin", "failed-logins"] as const,
  adminUsers: (f: AdminUserFilters) => ["admin", "users", f] as const,
  adminEvents: (kind: AdminEventKind | null) => ["admin", "events", kind] as const,
};

/** The signed-in user, or null when signed out (401 is an answer, not an error). */
export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async (): Promise<Me | null> => {
      const r = await api.GET("/api/v1/me");
      if (r.response.status === 401) return null;
      return unwrap(r);
    },
    staleTime: 60_000,
  });
}

/** What the sign-in screens need: whether sign-up is open, and the Turnstile site key. */
export function useAuthConfig() {
  return useQuery({
    queryKey: keys.authConfig,
    queryFn: async () => unwrap(await api.GET("/api/v1/auth/config")),
    staleTime: 5 * 60_000,
  });
}

export function useSources(enabled = true) {
  return useQuery({
    queryKey: keys.sources,
    queryFn: async () => unwrap(await api.GET("/api/v1/sources")),
    enabled,
    // Poll only while something is processing, and only while the tab is visible
    // (docs/UI_DESIGN.md §4.5: protect the Neon compute budget).
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status === "queued" || s.status === "syncing") ? 5000 : false,
    refetchIntervalInBackground: false,
  });
}

export function useRuns(sourceId: string) {
  return useQuery({
    queryKey: keys.runs(sourceId),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/sources/{source_id}/runs", { params: { path: { source_id: sourceId } } })),
  });
}

export function useSignOut() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const r = await api.POST("/api/v1/auth/logout");
      if (!r.response.ok && r.response.status !== 401) throw new ApiProblem({ type: "/problems/logout", title: "Couldn't sign out", status: r.response.status });
    },
    onSettled: () => client.clear(),
  });
}

/**
 * Hybrid search. Keyed by the whole request, so back/forward hits the cache. The previous
 * results stay on screen while a new query loads (UI_DESIGN §4.4: no flash to empty).
 * `elapsedMs` is measured here because the honest number is what the user waited.
 */
export function useSearch(body: SearchBody | null) {
  return useQuery({
    queryKey: keys.search(body),
    queryFn: async (): Promise<SearchOut & { elapsedMs: number }> => {
      const started = performance.now();
      const data = unwrap(await api.POST("/api/v1/search", { body: body! }));
      return { ...data, elapsedMs: Math.round(performance.now() - started) };
    },
    enabled: !!body?.query,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: false, // a 429 or validation error won't fix itself
  });
}

export function useDocument(id: string | null) {
  return useQuery({
    queryKey: keys.document(id ?? ""),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id! } } })),
    enabled: !!id,
    staleTime: 60_000,
  });
}

export type ChatSession = components["schemas"]["ChatSessionOut"];
export type ChatMessageOut = components["schemas"]["MessageOut"];

export const CHAT_SESSIONS_PAGE = 30;

/**
 * The signed-in user's chats, newest first, 30 at a time (cursor: the oldest created_at
 * seen). No polling: the list changes only when this tab starts a chat, and the chat store
 * inserts that row itself (Neon compute budget, UI_DESIGN §4.5).
 */
export function useChatSessions() {
  return useInfiniteQuery({
    queryKey: keys.chatSessions,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/chat/sessions", {
          params: { query: { limit: CHAT_SESSIONS_PAGE, ...(pageParam ? { before: pageParam } : {}) } },
        }),
      ),
    getNextPageParam: (last) => (last.length === CHAT_SESSIONS_PAGE ? last.at(-1)?.created_at : undefined),
    staleTime: 60_000,
  });
}

type SessionPages = { pages: ChatSession[][]; pageParams: unknown[] };

/** Apply a change to every cached page of the history list. */
function editSessions(qc: ReturnType<typeof useQueryClient>, edit: (s: ChatSession[]) => ChatSession[]) {
  qc.setQueryData<SessionPages>(keys.chatSessions, (old) => (old ? { ...old, pages: old.pages.map(edit) } : old));
}

/** Rename a chat (ADR-0044); the list shows the server's cleaned title. */
export function useRenameChat() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, title }: { id: string; title: string }) =>
      unwrap(await api.PATCH("/api/v1/chat/sessions/{session_id}", { params: { path: { session_id: id } }, body: { title } })),
    onSuccess: (saved) => editSessions(qc, (page) => page.map((s) => (s.id === saved.id ? { ...s, title: saved.title } : s))),
  });
}

/**
 * Delete a chat (ADR-0044): it leaves the list at once (optimistic) and comes back if the
 * server refuses. The server keeps it restorable for 24 hours, so Undo is real.
 */
export function useDeleteChat() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      // 204 No Content: success has no body to unwrap; only an error is read.
      const r = await api.DELETE("/api/v1/chat/sessions/{session_id}", { params: { path: { session_id: id } } });
      if (!r.response.ok) unwrap(r);
    },
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: keys.chatSessions });
      const before = qc.getQueryData<SessionPages>(keys.chatSessions);
      editSessions(qc, (page) => page.filter((s) => s.id !== id));
      return { before };
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.before) qc.setQueryData(keys.chatSessions, ctx.before);
    },
    onSettled: (_d, _e, id) => {
      qc.removeQueries({ queryKey: keys.chatMessages(id) });
    },
  });
}

export function useRestoreChat() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const r = await api.POST("/api/v1/chat/sessions/{session_id}/restore", { params: { path: { session_id: id } } });
      if (!r.response.ok) unwrap(r); // 204 No Content on success
    },
    onSettled: () => qc.invalidateQueries({ queryKey: keys.chatSessions }),
  });
}

/** A chat's stored messages (oldest first), to reopen it from the URL. */
export function useChatMessages(sessionId: string | null) {
  return useQuery({
    queryKey: keys.chatMessages(sessionId),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/chat/sessions/{session_id}/messages", { params: { path: { session_id: sessionId! } } })),
    enabled: !!sessionId,
    staleTime: Infinity, // new turns are appended locally while streaming
  });
}

export type GitHubRepo = components["schemas"]["RepoOut"];

/** Whether this server's GitHub App is configured (the GitHub tile is live only then). */
export function useGitHubStatus(enabled = true) {
  return useQuery({
    queryKey: ["github-status"] as const,
    queryFn: async () => unwrap(await api.GET("/api/v1/sources/github/status")),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export type DiscordChannel = components["schemas"]["DiscordChannelOut"];
export type DiscordServer = components["schemas"]["DiscordServerOut"];

/** Whether this server's Discord bot is configured (the Discord tile is live only then). */
export function useDiscordStatus(enabled = true) {
  return useQuery({
    queryKey: ["discord-status"] as const,
    queryFn: async () => unwrap(await api.GET("/api/v1/sources/discord/status")),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export type MailProvider = components["schemas"]["MailProviderOut"];

/** Whether mailboxes can be connected here, and which providers (the Email tile). */
export function useMailStatus(enabled = true) {
  return useQuery({
    queryKey: ["mail-status"] as const,
    queryFn: async () => unwrap(await api.GET("/api/v1/sources/email/status")),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export type Workspace = components["schemas"]["WorkspaceOut"];
export type Member = components["schemas"]["MemberOut"];
export type Invite = components["schemas"]["InviteOut"];

/** Workspaces this account belongs to (the switcher, ADR-0030). */
export function useWorkspaces() {
  return useQuery({
    queryKey: keys.workspaces,
    queryFn: async () => unwrap(await api.GET("/api/v1/workspaces")),
    staleTime: 60_000,
  });
}

export function useMembers() {
  return useQuery({
    queryKey: keys.members,
    queryFn: async () => unwrap(await api.GET("/api/v1/workspaces/members")),
  });
}

/** Open invitations; only admins and the owner may list them. */
export function useInvites(enabled: boolean) {
  return useQuery({
    queryKey: keys.invites,
    queryFn: async () => unwrap(await api.GET("/api/v1/workspaces/invites")),
    enabled,
  });
}

export type Activity = components["schemas"]["ActivityOut"];

/** Recent runs across visible sources. Polls only while one is running and the tab is
 * visible (UI_DESIGN §4.5: the Neon compute budget). */
export function useActivity() {
  return useQuery({
    queryKey: keys.activity,
    queryFn: async () => unwrap(await api.GET("/api/v1/runs", { params: { query: { limit: 15 } } })),
    refetchInterval: (query) => (query.state.data?.some((r) => r.status === "running") ? 5000 : false),
    refetchIntervalInBackground: false,
  });
}

/** Every integration and this user's state for each (ADR-0032). */
export function useIntegrations() {
  return useQuery({
    queryKey: keys.integrations,
    queryFn: async () => unwrap(await api.GET("/api/v1/integrations")),
    staleTime: 30_000,
  });
}

// ------------------------------------------------------------ admin console (ADR-0042)

export type AdminOverview = components["schemas"]["OverviewOut"];
export type AdminUser = components["schemas"]["AdminUserOut"];
export type AdminEvent = components["schemas"]["AuthEventOut"];
export type AdminEventKind = AdminEvent["kind"];
export type AdminRange = 30 | 90 | 365;
export type AdminUserStatus = "all" | "verified" | "unverified" | "disabled" | "active" | "inactive";
export type AdminUserSort = "joined" | "last_active" | "email";
export type AdminUserFilters = { q: string; status: AdminUserStatus; sort: AdminUserSort; order: "asc" | "desc" };

const ADMIN_PAGE = 25;

export function useAdminOverview() {
  return useQuery({
    queryKey: keys.adminOverview,
    queryFn: async () => unwrap(await api.GET("/api/v1/admin/overview")),
    staleTime: 60_000,
  });
}

export function useAdminSignups(days: AdminRange) {
  return useQuery({
    queryKey: keys.adminSignups(days),
    queryFn: async () => unwrap(await api.GET("/api/v1/admin/signups", { params: { query: { days } } })),
    staleTime: 60_000,
    placeholderData: keepPreviousData, // switching range keeps the old bars until the new ones land
  });
}

export function useAdminFailedLogins() {
  return useQuery({
    queryKey: keys.adminFailed,
    queryFn: async () => unwrap(await api.GET("/api/v1/admin/failed-logins", { params: { query: { days: 30 } } })),
    staleTime: 60_000,
  });
}

export function useAdminUsers(f: AdminUserFilters) {
  return useInfiniteQuery({
    queryKey: keys.adminUsers(f),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/admin/users", {
          params: { query: { ...f, limit: ADMIN_PAGE, ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData, // typing in search doesn't blank the table
  });
}

export function useAdminEvents(kind: AdminEventKind | null) {
  return useInfiniteQuery({
    queryKey: keys.adminEvents(kind),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/admin/events", {
          params: { query: { ...(kind ? { kind } : {}), ...(pageParam ? { cursor: pageParam } : {}) } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/** Disable or re-enable an account; refreshes every admin view on success. */
export function useSetUserDisabled() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, disabled }: { id: string; disabled: boolean }) =>
      unwrap(await api.PATCH("/api/v1/admin/users/{user_id}", { params: { path: { user_id: id } }, body: { disabled } })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.admin }),
  });
}

/** Permanently delete an account (the admin types its email to confirm). */
export function useDeleteUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, confirmEmail }: { id: string; confirmEmail: string }) => {
      const r = await api.POST("/api/v1/admin/users/{user_id}/delete", {
        params: { path: { user_id: id } },
        body: { confirm_email: confirmEmail },
      });
      if (!r.response.ok) unwrap(r);
    },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.admin }),
  });
}
