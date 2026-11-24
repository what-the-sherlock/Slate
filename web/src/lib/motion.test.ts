import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useClock, useCycle, useTypewriter } from "./motion";

function setReducedMotion(reduced: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduced && query.includes("reduce"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (hidden ? "hidden" : "visible") });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers();
  setReducedMotion(false);
  setHidden(false);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useCycle", () => {
  it("advances every interval and wraps around", () => {
    const { result } = renderHook(() => useCycle(3, 100));
    expect(result.current).toBe(0);
    act(() => vi.advanceTimersByTime(100));
    expect(result.current).toBe(1);
    act(() => vi.advanceTimersByTime(200));
    expect(result.current).toBe(0);
  });

  it("holds its step while paused", () => {
    const { result, rerender } = renderHook(({ paused }) => useCycle(3, 100, { paused }), { initialProps: { paused: false } });
    act(() => vi.advanceTimersByTime(100));
    rerender({ paused: true });
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(1);
  });

  it("stops while the tab is hidden", () => {
    const { result } = renderHook(() => useCycle(3, 100));
    act(() => setHidden(true));
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(0);
  });

  it("returns -1 (finished state) when motion is reduced", () => {
    setReducedMotion(true);
    const { result } = renderHook(() => useCycle(3, 100));
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(-1);
  });
});

describe("useTypewriter", () => {
  it("types one character per interval and stops at the end", () => {
    const { result } = renderHook(() => useTypewriter("abc", 10));
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(20));
    expect(result.current).toBe("ab");
    act(() => vi.advanceTimersByTime(100));
    expect(result.current).toBe("abc");
  });

  it("restarts when the text changes", () => {
    const { result, rerender } = renderHook(({ text }) => useTypewriter(text, 10), { initialProps: { text: "abc" } });
    act(() => vi.advanceTimersByTime(100));
    rerender({ text: "xyz" });
    expect(result.current).toBe("");
  });

  it("returns the full text at once when motion is reduced", () => {
    setReducedMotion(true);
    const { result } = renderHook(() => useTypewriter("hello", 10));
    expect(result.current).toBe("hello");
  });
});

describe("useClock", () => {
  it("advances in ticks, wraps at the end and can seek", () => {
    const { result } = renderHook(() => useClock(300, 100));
    expect(result.current.ms).toBe(0);
    act(() => vi.advanceTimersByTime(200));
    expect(result.current.ms).toBe(200);
    act(() => vi.advanceTimersByTime(100));
    expect(result.current.ms).toBe(0);
    act(() => result.current.seek(200));
    expect(result.current.ms).toBe(200);
  });

  it("holds while paused and reports -1 when motion is reduced", () => {
    const paused = renderHook(() => useClock(1000, 100, { paused: true }));
    act(() => vi.advanceTimersByTime(500));
    expect(paused.result.current.ms).toBe(0);
    setReducedMotion(true);
    const reduced = renderHook(() => useClock(1000, 100));
    expect(reduced.result.current.ms).toBe(-1);
  });
});
