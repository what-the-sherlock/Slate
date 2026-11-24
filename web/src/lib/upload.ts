import { api, ApiProblem, UNKNOWN_PROBLEM, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

/**
 * Browser side of the direct-to-storage upload (ADR-0016):
 * 1. hash the file (SHA-256, WebCrypto) so duplicates are caught before any bytes move;
 * 2. ask the API to begin -> presigned POST form (the bucket enforces size and type);
 * 3. send the file straight to storage, with progress;
 * 4. complete -> small files come back "indexed", bigger ones are queued for the worker.
 */

export type Visibility = components["schemas"]["BeginUploadBody"]["visibility"];

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
export const ACCEPTED = ".pdf,.docx,.md,.markdown,.txt,.html,.htm,.csv";
const ALLOWED_EXTENSIONS = ACCEPTED.split(",");

export type UploadPhase =
  | { kind: "hashing" }
  | { kind: "uploading"; progress: number }
  | { kind: "checking" }
  | { kind: "processing"; jobId: string }
  | { kind: "ready" }
  | { kind: "duplicate" }
  | { kind: "visibility_differs" }
  | { kind: "failed"; message: string };

/** Client-side pre-checks (the server re-checks everything; this is for fast feedback). */
export function precheck(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return "File type not supported";
  }
  if (file.size === 0) return "This file is empty";
  if (file.size > MAX_UPLOAD_BYTES) return "Larger than 25 MB";
  return null;
}

export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** POST a presigned form with upload progress (fetch can't report upload progress). */
export function postForm(
  url: string,
  fields: Record<string, string>,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [k, v] of Object.entries(fields)) form.append(k, v);
    form.append("file", file); // must be the last field
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.timeout = 10 * 60 * 1000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error("Storage rejected the file"));
    xhr.onerror = () => reject(new Error("Upload failed. Check your connection."));
    xhr.ontimeout = () => reject(new Error("Upload timed out."));
    xhr.send(form);
  });
}

export async function uploadFile(
  file: File,
  visibility: Visibility,
  onPhase: (phase: UploadPhase) => void,
): Promise<void> {
  const problem = precheck(file);
  if (problem) return onPhase({ kind: "failed", message: problem });

  onPhase({ kind: "hashing" });
  const sha256 = await sha256Hex(await file.arrayBuffer());

  const begun = unwrap(
    await api.POST("/api/v1/uploads", {
      body: { filename: file.name, size: file.size, sha256, visibility },
    }),
  );
  if (begun.status === "duplicate") return onPhase({ kind: "duplicate" });
  if (begun.status === "visibility_differs") return onPhase({ kind: "visibility_differs" });
  if (begun.status === "processing") return onPhase({ kind: "checking" });
  if (!begun.upload || !begun.upload_token) throw new Error("Unexpected response from the server.");

  onPhase({ kind: "uploading", progress: 0 });
  await postForm(begun.upload.url, begun.upload.fields, file, (p) => onPhase({ kind: "uploading", progress: p }));

  onPhase({ kind: "checking" });
  let done;
  try {
    done = unwrap(
      await api.POST("/api/v1/uploads/complete", { body: { upload_token: begun.upload_token } }),
    );
  } catch (err) {
    // The file is already in storage and the job may be running: a body that isn't one of
    // our problems came from a proxy/gateway (e.g. it timed out), not from the API.
    if (err instanceof ApiProblem && err.problem.type === UNKNOWN_PROBLEM && err.status >= 500) {
      throw new Error("The server took too long to answer. The file may still be indexing; check Sources shortly.");
    }
    throw err;
  }
  if (done.status === "indexed") return onPhase({ kind: "ready" });
  if (done.status === "duplicate") return onPhase({ kind: "duplicate" });
  if (done.job_id) return onPhase({ kind: "processing", jobId: done.job_id });
  onPhase({ kind: "checking" });
}
