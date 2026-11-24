/**
 * One story told across the three columns, on one clock (lib/motion useClock):
 * sources are connected → someone asks with @ → Slate thinks → a cited answer streams in.
 * Every moment is in milliseconds from the start of the loop. `ms === -1` means reduced motion:
 * render the finished frame.
 */
export const TICK = 50;
export const TOTAL = 18_000;

export const T = {
  // Column 2: ask
  typeStart: 3_000,
  question: "What's our leave policy ",
  typePerChar: 60,
  atSign: 4_500,
  menuOpen: 4_700,
  pointerIn: 4_900,
  pointerOnRow: 5_600,
  click: 5_900,
  chipIn: 6_050,
  sendReady: 6_300,
  send: 6_800,
  thinkStart: 7_000,
  stepEvery: 550,
  thoughtDone: 9_400,
  // Column 3: answer
  sourcesIn: 9_600,
  streamStart: 10_000,
  perWord: 150,
  chipPop: 12_500,
  actionsIn: 12_800,
  followUpIn: 13_200,
} as const;

/** Has the story reached `start`? Always true in the finished frame. */
export function reached(ms: number, start: number): boolean {
  return ms < 0 || ms >= start;
}

/** Which column is "on stage" right now (used by the phone tabs). */
export function stageOf(ms: number): 0 | 1 | 2 {
  if (ms < 0) return 0;
  if (ms < T.typeStart) return 0;
  if (ms < T.sourcesIn) return 1;
  return 2;
}

export const STAGE_START = [0, T.typeStart - 400, T.sourcesIn - 200] as const;
