import { afterEach, describe, expect, it, vi } from "vitest";
import { absoluteTime, relativeTime } from "./format";
import { MAX_UPLOAD_BYTES, precheck, sha256Hex, uploadFile, type UploadPhase } from "./upload";

function file(name: string, size: number): File {
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("precheck", () => {
  it("accepts supported types within the limit", () => {
    expect(precheck(file("handbook.PDF", 1000))).toBeNull();
    expect(precheck(file("notes.md", 10))).toBeNull();
  });

  it("rejects unsupported, empty and oversized files", () => {
    expect(precheck(file("setup.exe", 10))).toBe("File type not supported");
    expect(precheck(file("empty.txt", 0))).toBe("This file is empty");
    expect(precheck(file("big.pdf", MAX_UPLOAD_BYTES + 1))).toBe("Larger than 25 MB");
  });
});

describe("sha256Hex", () => {
  it("matches the known digest of 'abc'", async () => {
    const data = new TextEncoder().encode("abc").buffer as ArrayBuffer;
    expect(await sha256Hex(data)).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-11-24T12:00:00Z");
  it("formats recent and older times", () => {
    expect(relativeTime(null, now)).toBe("—");
    expect(relativeTime("2026-11-24T11:59:40Z", now)).toBe("Just now");
    expect(relativeTime("2026-11-24T11:46:00Z", now)).toBe("14 min ago");
  });
});

describe("absoluteTime", () => {
  it("includes the date, time and zone without throwing", () => {
    // Regression: dateStyle/timeStyle + timeZoneName throws "Invalid option" in Intl.
    const out = absoluteTime("2026-11-24T19:10:51Z");
    expect(out).toMatch(/2026/);
    expect(out).toMatch(/\d{1,2}:\d{2}/); // minutes depend on the machine's zone (IST is +5:30)
    expect(absoluteTime(null)).toBe("");
  });
});

describe("uploadFile", () => {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

  class FakeXhr {
    status = 204;
    upload = { onprogress: null };
    onload: (() => void) | null = null;
    open() {}
    send() {
      queueMicrotask(() => this.onload?.());
    }
  }

  afterEach(() => vi.unstubAllGlobals());

  it("explains a gateway timeout on complete instead of 'Unexpected response'", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (req: Request) =>
        req.url.endsWith("/uploads/complete")
          ? new Response("Internal Server Error", { status: 500 }) // what the dev proxy sends
          : json({ status: "upload", filename: "a.md", upload: { url: "https://s3.test/b", fields: {} }, upload_token: "tok_0123456789" }),
      ),
    );
    const phases: UploadPhase[] = [];
    const md = new File(["# hi"], "a.md");
    // jsdom's File has no arrayBuffer(); the hash only needs some bytes.
    Object.defineProperty(md, "arrayBuffer", { value: async () => new TextEncoder().encode("# hi").buffer });
    await expect(uploadFile(md, "only_me", (p) => phases.push(p))).rejects.toThrow(
      /took too long.*may still be indexing/,
    );
    expect(phases.at(-1)?.kind).toBe("checking"); // the bytes reached storage first
  });
});
