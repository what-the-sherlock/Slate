import { describe, expect, it } from "vitest";
import { repairMarkdown, splitBlocks } from "./markdown-stream";

describe("repairMarkdown", () => {
  it("closes an open code fence", () => {
    expect(repairMarkdown("Run:\n\n```bash\nuv sync")).toBe("Run:\n\n```bash\nuv sync\n```");
  });

  it("leaves a closed fence and finished text alone", () => {
    const done = "Run:\n\n```bash\nuv sync\n```\n\nThen **go** [1].";
    expect(repairMarkdown(done)).toBe(done);
  });

  it("closes unfinished bold instead of flickering", () => {
    expect(repairMarkdown("New parents get **16 we")).toBe("New parents get **16 we**");
  });

  it("drops a bold marker that opens nothing yet", () => {
    expect(repairMarkdown("Leave is **")).toBe("Leave is ");
  });

  it("closes inline code, and ignores ** inside it", () => {
    expect(repairMarkdown("Use `a ** b")).toBe("Use `a ** b`");
  });

  it.each([
    ["25 days [1", "25 days "],
    ["See [the handbook", "See "],
    ["See [docs](https://exa", "See "],
  ])("hides a citation or link still being written: %s", (input, expected) => {
    expect(repairMarkdown(input)).toBe(expected);
  });

  it("only touches the last paragraph", () => {
    expect(repairMarkdown("A **b** c.\n\nNext [2")).toBe("A **b** c.\n\nNext ");
  });
});

describe("splitBlocks", () => {
  it("splits on blank lines", () => {
    expect(splitBlocks("# Title\n\nPara one.\n\n- a\n- b")).toEqual(["# Title", "Para one.", "- a\n- b"]);
  });

  it("keeps a fenced code block whole, blank lines included", () => {
    expect(splitBlocks("Intro\n\n```py\na = 1\n\nb = 2\n```\n\nAfter")).toEqual(["Intro", "```py\na = 1\n\nb = 2\n```", "After"]);
  });

  it("treats an unfinished fence as one growing block", () => {
    expect(splitBlocks("```\nx\n\ny")).toEqual(["```\nx\n\ny"]);
  });
});
