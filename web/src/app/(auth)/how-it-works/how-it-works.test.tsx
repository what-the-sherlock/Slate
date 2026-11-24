import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ICONS } from "@/components/brand";
import { MARQUEE_CONNECTORS } from "./connect-marquee";
import { HowItWorks } from "./index";
import { stageOf, T, TOTAL } from "./timeline";

// Under reduced motion the clock is frozen at -1: every column shows its finished frame.
beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("reduce"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("HowItWorks", () => {
  it("gives every step a real heading and a sentence for screen readers", () => {
    render(<HowItWorks />);
    for (const name of [/connect anything/i, /ask with @/i, /get a cited answer/i]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("Pick sources with @. Slate only searches what you can see.")).toBeInTheDocument();
  });

  it("shows the finished story when motion is reduced", () => {
    render(<HowItWorks />);
    expect(document.body.textContent).toContain("One search box forGitHub");
    expect(screen.getByText(/tools and counting/)).toBeInTheDocument();
    expect(screen.getByText("Thought for 3 seconds")).toBeInTheDocument();
    expect(screen.getByText("Ranking passages")).toBeInTheDocument();
    expect(screen.getByText("3 sources")).toBeInTheDocument();
    // The answer streams word by word, so read the combined text.
    expect(document.body.textContent).toContain("16 weeks of paid leave, usable in blocks over the first year.");
    expect(screen.getByText("How do I request it?")).toBeInTheDocument();
    // The picked source travels from the question to the citation.
    expect(screen.getAllByText("Handbook.pdf").length).toBeGreaterThanOrEqual(2);
  });

  it("keeps every illustration away from assistive tech", () => {
    render(<HowItWorks />);
    expect(screen.getByText("Thought for 3 seconds").closest('[aria-hidden="true"]')).not.toBeNull();
    expect(screen.getByText("3 sources").closest('[aria-hidden="true"]')).not.toBeNull();
  });

  it("lets phone users pick a step even when motion is reduced", () => {
    render(<HowItWorks />);
    const third = screen.getByRole("button", { name: /step 3/i });
    expect(third).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(third);
    expect(third).toHaveAttribute("aria-pressed", "true");
  });
});

describe("timeline", () => {
  it("plays its beats in order and inside the loop", () => {
    const beats = [T.typeStart, T.atSign, T.menuOpen, T.pointerIn, T.pointerOnRow, T.click, T.chipIn, T.sendReady, T.send, T.thinkStart, T.thoughtDone, T.sourcesIn, T.streamStart, T.chipPop, T.actionsIn, T.followUpIn];
    expect([...beats].sort((a, b) => a - b)).toEqual(beats);
    expect(beats.at(-1)!).toBeLessThan(TOTAL);
  });

  it("hands the stage from connect to ask to answer", () => {
    expect(stageOf(0)).toBe(0);
    expect(stageOf(T.typeStart)).toBe(1);
    expect(stageOf(T.sourcesIn)).toBe(2);
  });
});

describe("marquee", () => {
  it("only uses brand marks the installed simple-icons package really has", () => {
    for (const key of MARQUEE_CONNECTORS) {
      if (key === "upload") continue;
      const icon = (ICONS as Record<string, { path: string; hex: string } | undefined>)[key];
      expect(icon?.path, key).toBeTruthy();
      expect(icon?.hex, key).toMatch(/^[0-9A-F]{6}$/i);
    }
  });
});
