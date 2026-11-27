import { describe, expect, it } from "vitest";
import { answerWithFootnotes, citationMarkdown, newTurn, reduceTurn, safeUrl, SseParser, turnsFromHistory, type SourceOut } from "./chat";

const SOURCE: SourceOut = {
  n: 1,
  chunk_id: 7,
  document_id: "00000000-0000-0000-0000-000000000001",
  ordinal: 0,
  title: "Handbook",
  heading_path: "Leave",
  snippet: "Everyone gets 25 days.",
};

describe("SseParser", () => {
  it("parses events split across chunks and ignores pings and unknown events", () => {
    const p = new SseParser();
    const got = [
      ...p.feed('event: token\ndata: {"v":1,"te'),
      ...p.feed('xt":"Hi"}\n\n: ping\n\nevent: weird\ndata: {}\n\n'),
      ...p.feed('event: done\r\ndata: {"v":1,"message_id":"m"}\r\n\r\n'),
    ];
    expect(got.map((e) => e.type)).toEqual(["token", "done"]);
    expect(got[0]).toEqual({ type: "token", data: { v: 1, text: "Hi" } });
  });

  it("drops malformed JSON instead of rendering it", () => {
    expect(new SseParser().feed("event: token\ndata: {not json\n\n")).toEqual([]);
  });
});

describe("reduceTurn", () => {
  it("follows the answer lifecycle", () => {
    let t = newTurn("How much leave?", "k");
    expect(t.stage).toBe("searching");
    t = reduceTurn(t, { type: "sources", data: { sources: [SOURCE] } });
    expect(t.stage).toBe("reading");
    t = reduceTurn(t, { type: "token", data: { text: "25 days [1]." } });
    t = reduceTurn(t, { type: "citation", data: { v: 1, n: 1, chunk_id: 7, document_id: SOURCE.document_id } });
    expect(t.stage).toBe("streaming");
    expect(t.cited).toEqual([1]);
    t = reduceTurn(t, {
      type: "done",
      data: { v: 1, message_id: "m", abstained: false, model: "x", latency_ms: 10, citations: [1], closest: [], notices: [], mode: "grounded" },
    });
    expect(t.stage).toBe("done");
    expect(t.messageId).toBe("m");
  });

  it("shows the closest matches when abstaining", () => {
    const t = reduceTurn(newTurn("q", "k"), {
      type: "done",
      data: { v: 1, message_id: "m", abstained: true, model: null, latency_ms: 5, citations: [], closest: [SOURCE], notices: [], mode: "grounded" },
    });
    expect(t.done?.abstained).toBe(true);
    expect(t.sources).toEqual([SOURCE]);
  });
});

describe("citationMarkdown", () => {
  it("links only server-validated markers and escapes the rest", () => {
    expect(citationMarkdown("A [1]. B [9].", [1])).toBe("A [1](cite:1). B \\[9\\].");
  });
});

describe("safeUrl", () => {
  it.each([
    ["cite:2", "cite:2"],
    ["https://example.com/x", "https://example.com/x"],
    ["javascript:alert(1)", ""],
    ["data:text/html,hi", ""],
    ["/relative", ""],
  ])("%s -> %s", (url, expected) => {
    expect(safeUrl(url)).toBe(expected);
  });
});

it("copies the answer with footnotes for cited sources only", () => {
  const t = { ...newTurn("q", "k"), text: "25 days [1].", sources: [SOURCE, { ...SOURCE, n: 2, title: "Other" }], cited: [1] };
  expect(answerWithFootnotes(t)).toBe("25 days [1].\n\n[1] Handbook");
});

it("never lets the model mint its own citation links", () => {
  expect(citationMarkdown("See [this](cite:7) and [1].", [1])).toBe("See [this](#7) and [1](cite:1).");
});

it("a reopened chat keeps real source names and follow-ups, and drops revoked ones", () => {
  const at = "2026-11-28T10:00:00Z";
  const [turn] = turnsFromHistory([
    { id: "u1", role: "user", content: "Leave?", created_at: at, model: null, latency_ms: null, abstained: false, mode: "grounded", citations: [], followups: [] },
    {
      id: "a1",
      role: "assistant",
      content: "25 days [1][2].",
      created_at: at,
      model: "m",
      latency_ms: 10,
      abstained: false,
      mode: "grounded",
      followups: ["Who approves leave?"],
      citations: [
        { n: 1, chunk_id: 7, document_id: "00000000-0000-0000-0000-000000000001", title: "Handbook", heading_path: "Leave", snippet: "25 days.", connector: "upload" },
        { n: 2, chunk_id: 8, document_id: "00000000-0000-0000-0000-000000000002" },
      ],
    },
  ]);
  expect(turn!.sources.map((s) => s.title)).toEqual(["Handbook"]);
  expect(turn!.sources[0]!.snippet).toBe("25 days.");
  expect(turn!.done?.followups).toEqual(["Who approves leave?"]);
});
