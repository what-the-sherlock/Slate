import { describe, expect, it } from "vitest";
import { relativeTime } from "./format";
import { MAX_UPLOAD_BYTES, precheck, sha256Hex } from "./upload";

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
