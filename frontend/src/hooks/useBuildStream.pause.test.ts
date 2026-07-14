import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentHandle } from "@/api/agent";
import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

const sent: WSClientFrame[] = [];

vi.mock("@/api/agent", () => ({
  subscribeConversation: (
    _cid: string,
    _onFrame: (frame: WSServerFrame) => void,
  ): AgentHandle => {
    void _cid;
    void _onFrame;
    return {
      send: (frame: WSClientFrame) => sent.push(frame),
      cancel: () => {},
    };
  },
}));

afterEach(() => sent.splice(0));

describe("useBuildStream — cooperative stop", () => {
  it("sends the lock-free resumable pause control", () => {
    const session = { cid: "c_pause", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => result.current.cancel());

    expect(sent.at(-1)).toEqual({ type: "pause" });
    expect(sent).not.toContainEqual({ type: "cancel" });
  });
});
