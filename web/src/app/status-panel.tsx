"use client";

import { useQuery } from "@tanstack/react-query";
import { api, ApiProblem, unwrap } from "@/lib/api/client";

type Row = { label: string; ok: boolean | undefined; detail: string };

function useHealth() {
  return useQuery({
    queryKey: ["healthz"],
    queryFn: async () => unwrap(await api.GET("/api/v1/healthz")),
  });
}

function useReadiness() {
  return useQuery({
    queryKey: ["readyz"],
    // 503 "not ready" still carries a useful body, so read it instead of throwing.
    queryFn: async () => {
      const r = await api.GET("/api/v1/readyz");
      if (r.data) return r.data;
      if (r.response.status === 503 && r.error && typeof r.error === "object") {
        return r.error as unknown as NonNullable<typeof r.data>;
      }
      return unwrap(r);
    },
  });
}

export function StatusPanel() {
  const health = useHealth();
  const ready = useReadiness();

  const rows: Row[] = [
    {
      label: "API",
      ok: health.isPending ? undefined : health.isSuccess,
      detail: health.data ? `v${health.data.version}` : errorText(health.error),
    },
    {
      label: "Database",
      ok: ready.isPending ? undefined : ready.data?.database === true,
      detail: ready.data ? (ready.data.database ? "Connected" : "Unreachable") : errorText(ready.error),
    },
    {
      label: "Migrations",
      ok: ready.isPending ? undefined : ready.data?.migrations_current === true,
      detail: ready.data
        ? `at ${ready.data.migration_revision ?? "none"} · expected ${ready.data.expected_revision ?? "none"}`
        : errorText(ready.error),
    },
  ];

  return (
    <ul className="divide-y divide-border-subtle rounded-lg border border-border" aria-live="polite">
      {rows.map((row) => (
        <li key={row.label} className="flex items-center justify-between gap-4 px-4 py-3">
          <span className="font-medium text-fg">{row.label}</span>
          <span className="flex items-center gap-2 text-[13px] text-fg-subtle">
            <span className="tabular-nums">{row.detail}</span>
            <StatusPill ok={row.ok} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function StatusPill({ ok }: { ok: boolean | undefined }) {
  if (ok === undefined) {
    return (
      <span className="rounded-sm bg-emphasis px-2.5 py-1 text-[13px] font-medium text-fg">
        Checking
      </span>
    );
  }
  return ok ? (
    <span className="rounded-sm bg-success-subtle px-2.5 py-1 text-[13px] font-medium text-success">
      OK
    </span>
  ) : (
    <span className="rounded-sm bg-danger-subtle px-2.5 py-1 text-[13px] font-medium text-danger">
      Down
    </span>
  );
}

function errorText(error: unknown): string {
  if (!error) return "…";
  if (error instanceof ApiProblem) {
    return error.problem.request_id ? `${error.problem.title} (${error.problem.request_id})` : error.problem.title;
  }
  return "Couldn't reach the API";
}
