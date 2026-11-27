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
