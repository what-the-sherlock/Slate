"use client";

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/**
 * Follow a growing conversation only while the reader is at the bottom (UI_DESIGN §4.3).
 *
 * - A ResizeObserver on the content keeps the newest text in view as it streams, but only
 *   while `following`.
 * - Any *user* scroll up (wheel, touch, keys, scrollbar drag) stops following, so reading
 *   an earlier answer is never interrupted. Scrolling back to the bottom resumes it.
 * - `atBottom` drives the "Jump to latest" pill; `scrollToBottom` is for send and the pill.
 */
const THRESHOLD = 64; // px from the bottom that still counts as "at the bottom"

export function useStickToBottom(scrollRef: RefObject<HTMLElement | null>, contentRef: RefObject<HTMLElement | null>) {
  const following = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const programmatic = useRef(false);

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const el = scrollRef.current;
      if (!el) return;
      following.current = true;
      programmatic.current = true;
      el.scrollTo?.({ top: el.scrollHeight, behavior });
      setAtBottom(true);
      // A smooth scroll fires scroll events for a while; they're ours, not the user's.
      window.setTimeout(() => (programmatic.current = false), behavior === "smooth" ? 400 : 0);
    },
    [scrollRef],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = () => el.scrollHeight - el.scrollTop - el.clientHeight;
    const onScroll = () => {
      const bottom = distance() <= THRESHOLD;
      setAtBottom(bottom);
      if (bottom) following.current = true;
      else if (!programmatic.current) following.current = false;
    };
    const onIntent = (e: Event) => {
      // Upward intent stops following immediately, before the scroll event lands.
      if (e instanceof WheelEvent && e.deltaY < 0) following.current = false;
      if (e instanceof KeyboardEvent && ["ArrowUp", "PageUp", "Home"].includes(e.key)) following.current = false;
      if (e.type === "touchmove") following.current = distance() <= THRESHOLD;
      programmatic.current = false;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onIntent, { passive: true });
    el.addEventListener("touchmove", onIntent, { passive: true });
    el.addEventListener("keydown", onIntent);
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onIntent);
      el.removeEventListener("touchmove", onIntent);
      el.removeEventListener("keydown", onIntent);
    };
  }, [scrollRef]);

  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!following.current) return;
      programmatic.current = true;
      el.scrollTop = el.scrollHeight;
      requestAnimationFrame(() => (programmatic.current = false));
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [scrollRef, contentRef]);

  return { atBottom, scrollToBottom };
}
