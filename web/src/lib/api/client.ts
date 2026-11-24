import createClient, { type Middleware } from "openapi-fetch";
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
  credentials: "same-origin",
});

const CSRF_COOKIES = ["slate_csrf", "__Host-slate_csrf"];

export function readCsrfToken(
  cookieString: string = typeof document === "undefined" ? "" : document.cookie,
): string {
  for (const part of cookieString.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name && CSRF_COOKIES.includes(name)) return decodeURIComponent(rest.join("="));
  }
  return "";
}

/**
 * Security headers the API requires (docs/SECURITY.md):
 * - X-Requested-With on every request (pre-login CSRF defence);
 * - X-CSRF-Token, copied from the readable CSRF cookie, on every state-changing request.
 */
export const securityHeaders: Middleware = {
  onRequest({ request }) {
    request.headers.set("x-requested-with", "slate");
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      const token = readCsrfToken();
      if (token) request.headers.set("x-csrf-token", token);
    }
    return request;
  },
};
api.use(securityHeaders);

const problemSchema = z.object({
  type: z.string(),
  title: z.string(),
  status: z.number().int(),
  detail: z.string().optional(),
  instance: z.string().optional(),
  request_id: z.string().nullable().optional(),
  fields: z.record(z.string(), z.string()).optional(),
  retry_after_s: z.number().optional(),
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

  /** Field-level messages (e.g. {email: "Enter an email like…"}), if any. */
  get fields(): Record<string, string> {
    return this.problem.fields ?? {};
  }
}

/** Type of a problem synthesized here because the body wasn't one of the API's problems. */
export const UNKNOWN_PROBLEM = "/problems/unknown";

/** Turn an unknown error body into an ApiProblem (bodies are untrusted, so validate). */
export function toApiProblem(body: unknown, status: number): ApiProblem {
  const parsed = problemSchema.safeParse(body);
  if (parsed.success) return new ApiProblem(parsed.data);
  return new ApiProblem({
    type: UNKNOWN_PROBLEM,
    title: status >= 500 ? `The server didn't respond properly (HTTP ${status}). Try again shortly.` : "Unexpected response",
    status,
  });
}

/** Unwrap an openapi-fetch result: return data or throw an ApiProblem. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) return result.data;
  throw toApiProblem(result.error, result.response.status);
}

/** A short, user-facing message for any error (never internals). */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiProblem) {
    const { detail, title, request_id, status } = error.problem;
    const text = detail ?? title;
    return status >= 500 && request_id ? `${text} Reference: ${request_id}` : text;
  }
  if (error instanceof Error && error.message) return error.message;
  return "Couldn't reach the server. Check your connection and try again.";
}
