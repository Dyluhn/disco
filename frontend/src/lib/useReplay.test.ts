/**
 * RP-06 — useReplay hook unit tests.
 *
 * Verifies: cursor starts at tail in non-live mode; stepFwd/stepBack bounds;
 * seek clamping; atLive detection; live auto-tracking to tail;
 * no reducer dependency.
 */

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useReplay } from "@/lib/useReplay";
import type { AgentEvent } from "@/types/agent";

function ev(id: string): AgentEvent {
  return {
    id,
    kind: "status",
    status: "RUNNING",
    source: "system",
  } as AgentEvent;
}

function makeEvents(n: number): AgentEvent[] {
  return Array.from({ length: n }, (_, i) => ev(`evt_${i}`));
}

describe("useReplay", () => {
  it("starts at tail (max) when live=false", () => {
    const events = makeEvents(5);
    const { result } = renderHook(() => useReplay(events, false));
    expect(result.current.position).toBe(5);
    expect(result.current.max).toBe(5);
    expect(result.current.atLive).toBe(true);
  });

  it("starts at 0 when live=true (will track to tail via effect)", () => {
    const events = makeEvents(3);
    const { result } = renderHook(() => useReplay(events, true));
    // The effect runs and pushes to max
    expect(result.current.position).toBe(3);
    expect(result.current.max).toBe(3);
  });

  it("atLive is true when position equals max, zero events = live", () => {
    const { result } = renderHook(() => useReplay([], false));
    expect(result.current.atLive).toBe(true);
    expect(result.current.max).toBe(0);
  });

  it("stepBack retreats from tail and atLive becomes false", () => {
    const events = makeEvents(5);
    const { result } = renderHook(() => useReplay(events, false));
    expect(result.current.position).toBe(5);
    act(() => result.current.stepBack());
    expect(result.current.position).toBe(4);
    expect(result.current.atLive).toBe(false);
  });

  it("stepFwd advances one event", () => {
    const events = makeEvents(5);
    const { result } = renderHook(() => useReplay(events, false));
    act(() => result.current.stepBack());
    act(() => result.current.stepBack());
    expect(result.current.position).toBe(3);
    act(() => result.current.stepFwd());
    expect(result.current.position).toBe(4);
  });

  it("stepFwd clamps at max", () => {
    const events = makeEvents(3);
    const { result } = renderHook(() => useReplay(events, false));
    act(() => result.current.seek(2));
    act(() => result.current.stepFwd());
    act(() => result.current.stepFwd());
    act(() => result.current.stepFwd()); // should clamp
    expect(result.current.position).toBe(3);
    expect(result.current.atLive).toBe(true);
  });

  it("stepBack clamps at 0", () => {
    const events = makeEvents(3);
    const { result } = renderHook(() => useReplay(events, false));
    // start at 3, go back to 0
    act(() => result.current.seek(0));
    act(() => result.current.stepBack());
    expect(result.current.position).toBe(0);
  });

  it("seek clamps to valid range", () => {
    const events = makeEvents(5);
    const { result } = renderHook(() => useReplay(events, false));
    act(() => result.current.seek(-3));
    expect(result.current.position).toBe(0);
    act(() => result.current.seek(99));
    expect(result.current.position).toBe(5);
    act(() => result.current.seek(3));
    expect(result.current.position).toBe(3);
  });

  it("atLive tracks position vs max", () => {
    const events = makeEvents(4);
    const { result } = renderHook(() => useReplay(events, false));
    expect(result.current.atLive).toBe(true); // starts at tail
    act(() => result.current.seek(2));
    expect(result.current.atLive).toBe(false);
    act(() => result.current.seek(4));
    expect(result.current.atLive).toBe(true);
  });

  it("auto-tracks tail when live=true", () => {
    const events = makeEvents(3);
    const { result, rerender } = renderHook(
      ({ e, l }) => useReplay(e, l),
      { initialProps: { e: events, l: false } },
    );
    // Position starts at tail (3)
    expect(result.current.position).toBe(3);

    // Scrub back
    act(() => result.current.seek(1));
    expect(result.current.position).toBe(1);

    // Switch to live — should jump to tail
    rerender({ e: events, l: true });
    expect(result.current.position).toBe(3);
    expect(result.current.atLive).toBe(true);
  });

  it("stays at tail when live and new events arrive", () => {
    const { result, rerender } = renderHook(
      ({ e, l }) => useReplay(e, l),
      { initialProps: { e: makeEvents(2), l: true } },
    );
    expect(result.current.position).toBe(2);
    rerender({ e: makeEvents(5), l: true });
    expect(result.current.position).toBe(5);
  });

  it("jumps to tail when events grow in non-live mode (resume / first load)", () => {
    const events = makeEvents(3);
    const { result, rerender } = renderHook(
      ({ e, l }) => useReplay(e, l),
      { initialProps: { e: events, l: false } },
    );
    // Starts at tail (3)
    expect(result.current.position).toBe(3);

    // Scrub back
    act(() => result.current.seek(1));
    expect(result.current.position).toBe(1);

    // Events grow (e.g. resume added events)
    const more = makeEvents(7);
    rerender({ e: more, l: false });
    // Position jumps to new tail
    expect(result.current.position).toBe(7);
    expect(result.current.max).toBe(7);
  });

  it("stays at user position when events DON'T grow in non-live mode", () => {
    const events = makeEvents(5);
    const { result, rerender } = renderHook(
      ({ e, l }) => useReplay(e, l),
      { initialProps: { e: events, l: false } },
    );
    act(() => result.current.seek(2));
    // Re-render with same events — position should not jump
    rerender({ e: makeEvents(5), l: false });
    expect(result.current.position).toBe(2);
  });

  it("stays at 0 when user scrubs to 0 and events don't grow", () => {
    const events = makeEvents(5);
    const { result } = renderHook(() => useReplay(events, false));
    act(() => result.current.seek(0));
    expect(result.current.position).toBe(0);
  });
});
