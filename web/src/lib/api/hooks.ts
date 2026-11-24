"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiProblem, unwrap } from "./client";
import type { components } from "./schema";

export type Me = components["schemas"]["MeResponse"];
export type Source = components["schemas"]["SourceOut"];
export type Run = components["schemas"]["RunOut"];
export type JobState = components["schemas"]["JobOut"];

export const keys = {
  me: ["me"] as const,
  status: ["auth-status"] as const,
  sources: ["sources"] as const,
  runs: (id: string) => ["runs", id] as const,
  job: (id: string) => ["job", id] as const,
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
