/**
 * Rendering Markdown while it is still arriving (UI_DESIGN §4.3).
 *
 * A half-written answer is usually *invalid* Markdown: an open ``` fence turns the rest of
 * the page into code, an unclosed ** makes text flicker bold-then-plain, and a link or
 * citation cut mid-way shows its raw brackets for a frame. `repairMarkdown` closes what's
 * open and hides a dangling tail, so every frame renders like the finished answer will.
 *
 * `splitBlocks` cuts the text into top-level blocks (blank-line separated, fences kept
 * whole). Finished blocks never change again, so the renderer memoises them and only the
 * last block re-renders per token: no flicker, and long answers stay cheap.
 *
 * Pure functions: unit-tested in markdown-stream.test.ts.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** A fence closes an open one when it uses the same character, is at least as long, and stands alone. */
function closes(marker: string, open: string, line: string): boolean {
  return marker[0] === open[0] && marker.length >= open.length && line.trim() === marker;
}

/** Is the text currently inside an open fenced code block? Returns the opening fence. */
function openFence(text: string): string | null {
  let open: string | null = null;
  for (const line of text.split("\n")) {
    const marker = FENCE.exec(line)?.[1];
    if (!marker) continue;
    if (open === null) open = marker;
    else if (closes(marker, open, line)) open = null;
  }
  return open;
}

function countUnescaped(text: string, token: string): number {
  let n = 0;
  for (let i = text.indexOf(token); i !== -1; i = text.indexOf(token, i + token.length)) {
    if (text[i - 1] !== "\\") n++;
  }
  return n;
}

/** Close or hide whatever a partial answer leaves open. Idempotent on finished Markdown. */
export function repairMarkdown(text: string): string {
  const fence = openFence(text);
  if (fence) return `${text}\n${fence}`; // inside code nothing else applies

  // Only the last paragraph can be mid-construct.
  const cut = text.lastIndexOf("\n\n");
  const head = cut === -1 ? "" : text.slice(0, cut + 2);
  let tail = cut === -1 ? text : text.slice(cut + 2);

  // A citation or link still being written: "[1", "[see the docs", "[docs](https://exa"
  tail = tail.replace(/\[[^\]\n]*\]\([^)\n]*$/, "").replace(/\[[^\]\n]*$/, "");

  // Inline code first: what's inside backticks isn't emphasis.
  if (countUnescaped(tail, "`") % 2 === 1) tail += "`";
  const outsideCode = tail.replace(/`[^`]*`/g, "");
  if (countUnescaped(outsideCode, "**") % 2 === 1) {
    // "**" right at the end opens nothing visible yet: drop it instead of closing it.
    tail = /\*\*\s*$/.test(tail) ? tail.replace(/\*\*\s*$/, "") : `${tail.trimEnd()}**`;
  }
  return head + tail;
}

/** Top-level blocks: split on blank lines, never inside a fenced code block. */
export function splitBlocks(text: string): string[] {
  const blocks: string[] = [];
  let current: string[] = [];
  let fence: string | null = null;
  for (const line of text.split("\n")) {
    const marker = FENCE.exec(line)?.[1];
    if (marker) {
      if (fence === null) fence = marker;
      else if (closes(marker, fence, line)) fence = null;
    }
    if (fence === null && line.trim() === "" && current.length) {
      blocks.push(current.join("\n"));
      current = [];
      continue;
    }
    if (fence === null && line.trim() === "") continue;
    current.push(line);
  }
  if (current.length) blocks.push(current.join("\n"));
  return blocks;
}
