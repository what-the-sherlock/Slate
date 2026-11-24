import { describe, expect, it } from "vitest";
import type { Source } from "@/lib/api/hooks";
import { summarise } from "./home-view";

function source(over: Partial<Source>): Source {
  return {
    id: crypto.randomUUID(),
    name: "Handbook",
    connector: "upload",
    status: "synced",
    visibility: "workspace",
    data_class: "private",
    documents: 0,
    last_error: null,
    last_synced_at: null,
    ...over,
  } as Source;
}

describe("summarise", () => {
  it("is all zeros with no last sync for an empty workspace", () => {
    expect(summarise([])).toEqual({ count: 0, documents: 0, processing: 0, failed: 0, lastSynced: null });
  });

  it("totals documents, counts queued + syncing as processing, and picks the latest sync", () => {
    const s = summarise([
      source({ documents: 3, last_synced_at: "2026-11-20T10:00:00Z" }),
      source({ documents: 5, status: "syncing", last_synced_at: "2026-11-24T09:00:00Z" }),
      source({ documents: 0, status: "queued" }),
      source({ documents: 2, status: "failed", last_synced_at: "2026-11-01T00:00:00Z" }),
    ]);
    expect(s).toEqual({ count: 4, documents: 10, processing: 2, failed: 1, lastSynced: "2026-11-24T09:00:00Z" });
  });

  it("ignores sources that never synced when finding the last sync", () => {
    expect(summarise([source({ last_synced_at: null })]).lastSynced).toBeNull();
  });
});
