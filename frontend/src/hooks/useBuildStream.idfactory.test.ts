/**
 * #25 regression: the optimistic-message id factory is injectable, so a test can
 * FREEZE the otherwise non-deterministic Date.now()/Math.random() suffix. The
 * default (production) path is unchanged; only an explicitly-passed `idFactory`
 * overrides it. This proves the DOM key for an optimistic steer is reproducible
 * under a fixed factory — it does NOT prove steering works end-to-end (that needs
 * a live model).
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentHandle } from "@/api/agent";
import type { MessageEvent, WSClientFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

const sent: WSClientFrame[] = [];

vi.mock("@/api/agent", () => ({
  subscribeConversation: (): AgentHandle => ({
    send: (f: WSClientFrame) => sent.push(f),
    cancel: () => {},
  }),
  resumeConversation: () => Promise.resolve(),
}));

afterEach(() => {
  sent.length = 0;
});

describe("useBuildStream — injectable optimistic id factory (#25)", () => {
  it("uses the frozen factory for the optimistic steer id", () => {
    const session = { cid: "c_id", task: "build" };
    let n = 0;
    const { result } = renderHook(() =>
      useBuildStream(session, { idFactory: () => `frozen-${n++}` }),
    );

    act(() => {
      result.current.steer("focus on the header");
    });

    const optimistic = result.current.events.find(
      (e): e is MessageEvent => e.kind === "message" && e.source === "user",
    );
    expect(optimistic?.id).toBe("local-pending-frozen-0");
  });

  it("defaults to a non-frozen id when no factory is injected (production path)", () => {
    const session = { cid: "c_def", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      result.current.steer("hello");
    });

    const optimistic = result.current.events.find(
      (e): e is MessageEvent => e.kind === "message" && e.source === "user",
    );
    expect(optimistic?.id).toMatch(/^local-pending-/);
    expect(optimistic?.id).not.toBe("local-pending-frozen-0");
  });
});
