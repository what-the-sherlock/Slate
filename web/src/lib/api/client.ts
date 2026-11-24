import createClient from "openapi-fetch";
import { z } from "zod";
import type { paths } from "./schema";

/**
 * Typed API client. Types come from openapi.json (`pnpm gen:api`), so a backend change
 * that breaks the contract fails `pnpm typecheck` instead of failing at runtime.
 *
 * Requests go to the same origin: Next rewrites /api/* to the API (next.config.ts),
 * which keeps session cookies first-party and avoids CORS.
 */
export const api = createClient<paths>({
  // Absolute origin: browsers resolve relative URLs, but Node's Request (tests, SSR) can't.
  baseUrl: typeof window === "undefined" ? "" : window.location.origin,
  // Resolve fetch per call (not once at import) so instrumentation and test mocks apply.
  fetch: (request: Request) => globalThis.fetch(request),
});

const problemSchema = z.object({
  type: z.string(),
  title: z.string(),
  status: z.number().int(),
  detail: z.string().optional(),
  instance: z.string().optional(),
  request_id: z.string().nullable().optional(),
});

export type Problem = z.infer<typeof problemSchema>;

/** An RFC 9457 problem returned by the API. */
export class ApiProblem extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title);
    this.name = "ApiProblem";
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

/** Turn an unknown error body into an ApiProblem (bodies are untrusted, so validate). */
export function toApiProblem(body: unknown, status: number): ApiProblem {
  const parsed = problemSchema.safeParse(body);
  if (parsed.success) return new ApiProblem(parsed.data);
  return new ApiProblem({
    type: "/problems/unknown",
    title: "Unexpected response",
    status,
  });
}

/** Unwrap an openapi-fetch result: return data or throw an ApiProblem. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) return result.data;
  throw toApiProblem(result.error, result.response.status);
}
