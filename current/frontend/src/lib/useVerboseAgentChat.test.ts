/**
 * useVerboseAgentChat (W-43) — the localStorage-backed chat-verbosity preference.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  readVerboseAgentChat,
  setVerboseAgentChat,
  useVerboseAgentChat,
} from "@/lib/useVerboseAgentChat";

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  window.localStorage.clear();
});

describe("useVerboseAgentChat", () => {
  it("defaults to OFF (quiet mode) when unset", () => {
    expect(readVerboseAgentChat()).toBe(false);
    const { result } = renderHook(() => useVerboseAgentChat());
    expect(result.current.verbose).toBe(false);
  });

  it("persists OFF and reads back false", () => {
    setVerboseAgentChat(false);
    expect(window.localStorage.getItem("verboseAgentChat")).toBe("0");
    expect(readVerboseAgentChat()).toBe(false);
  });

  it("updates subscribed components live when toggled", () => {
    const { result } = renderHook(() => useVerboseAgentChat());
    expect(result.current.verbose).toBe(false);
    act(() => result.current.setVerbose(true));
    expect(result.current.verbose).toBe(true);
    act(() => result.current.setVerbose(false));
    expect(result.current.verbose).toBe(false);
  });
});
