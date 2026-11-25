"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  status: ["auth-status"] as const,
  sources: ["sources"] as const,
  runs: (id: string) => ["runs", id] as const,
  job: (id: string) => ["job", id] as const,
  search: (body: SearchBody | null) => ["search", body] as const,
  document: (id: string) => ["document", id] as const,
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

export function useSetupStatus() {
  return useQuery({
    queryKey: keys.status,
    queryFn: async () => unwrap(await api.GET("/api/v1/auth/status")),
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
