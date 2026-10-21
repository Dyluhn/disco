import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AudioStreamEvent } from "@/api/deepResearch";
import { useAudioOverview } from "./useAudioOverview";

const transport = vi.hoisted(() => ({ stream: vi.fn(), blocking: vi.fn() }));
vi.mock("@/api/deepResearch", () => ({
  absoluteAudioUrl: (url: string) => url,
  fetchExistingReportAudio: () => Promise.resolve([]),
  streamReportAudio: (...args: unknown[]) => transport.stream(...args),
  requestReportAudioBlocking: (...args: unknown[]) => transport.blocking(...args),
}));
beforeEach(() => vi.clearAllMocks());

describe("audio selection races", () => {
  it("ignores older stream progress, completion and fallback after a newer selection", async () => {
    const callbacks: Array<(event: AudioStreamEvent) => boolean> = [];
    const finish: Array<(value: boolean) => void> = [];
    transport.stream.mockImplementation((_cid, _mode, _body, callback) => {
      callbacks.push(callback);
      return new Promise<boolean>((resolve) => finish.push(resolve));
    });
    const { result } = renderHook(() => useAudioOverview({ cid: "conv_race", onModeOpenChange: vi.fn() }));
    let first!: Promise<void>, second!: Promise<void>;
    act(() => { first = result.current.generate("podcast"); });
    act(() => { second = result.current.generate("single"); });
    await act(async () => {
      callbacks[1]({ stage: "done", mode: "single", mp3_url: "/single.mp3" });
      finish[1](true); await second;
    });
    act(() => { callbacks[0]({ stage: "synthesizing", current: 5, total: 10 }); });
    expect(result.current.audio).toMatchObject({ status: "done", mode: "single" });
    await act(async () => {
      callbacks[0]({ stage: "done", mode: "podcast", mp3_url: "/podcast.mp3" });
      finish[0](false); await first;
    });
    expect(result.current.audio).toMatchObject({ status: "done", mode: "single", audioUrl: "/single.mp3" });
    expect(transport.blocking).not.toHaveBeenCalled();
  });
  it("retires old conversation callbacks when navigating to another report", async () => {
    let callback!: (event: AudioStreamEvent) => boolean;
    transport.stream.mockImplementation((_cid, _mode, _body, cb) => { callback = cb; return new Promise(() => {}); });
    const { result, rerender } = renderHook(({ cid }) => useAudioOverview({ cid, onModeOpenChange: vi.fn() }), { initialProps: { cid: "old" } });
    act(() => { void result.current.generate("podcast"); });
    rerender({ cid: "new" });
    act(() => { callback({ stage: "done", mode: "podcast", mp3_url: "/old.mp3" }); });
    await waitFor(() => expect(result.current.audio.status).toBe("idle"));
  });
});
