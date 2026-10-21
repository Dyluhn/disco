import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useLiveBrowserSession } from "./useLiveBrowserSession";
import type { PreviewLaunch } from "@/api/canvas";

const { mint, start, stop, ready } = vi.hoisted(() => ({
  mint: vi.fn(), start: vi.fn(), stop: vi.fn(), ready: vi.fn(),
}));
vi.mock("@/hooks/useModels", () => ({ useLiveBrowserConfig: () => ({ data: { enabled: true } }) }));
vi.mock("@/api/canvas", () => ({
  mintLiveBrowserLaunch: mint, startLiveBrowserSession: start, stopLiveBrowserSession: stop,
  getLiveBrowserReadiness: ready, touchLiveBrowserSession: vi.fn(),
}));
const first = { url: "http://preview/bootstrap", intent: "first", accessDeadline: 1 };
beforeEach(() => {
  vi.clearAllMocks();
  mint.mockResolvedValue(first);
  ready.mockResolvedValue({ ready: true });
  start.mockResolvedValue({ ready: true, port: 6080, novnc_path: "/vnc.html?view_only=1" });
});

describe("noVNC explicit access reload", () => {
  it("mints once without restarting the app and preserves the current launch on failure", async () => {
    const { result } = renderHook(() => useLiveBrowserSession("cid"));
    await waitFor(() => expect(result.current.liveView?.launch).toBe(first));
    mint.mockResolvedValueOnce({ ...first, intent: "second" });
    await act(async () => { await result.current.reloadPreview(); });
    expect(mint).toHaveBeenCalledTimes(2);
    expect(mint).toHaveBeenLastCalledWith("cid", 6080, "/vnc.html?view_only=1");
    expect(start).toHaveBeenCalledTimes(1);
    expect(stop).not.toHaveBeenCalled();
    expect(result.current.liveView?.launch.intent).toBe("second");
    mint.mockRejectedValueOnce(new Error("offline"));
    await act(async () => { await expect(result.current.reloadPreview()).rejects.toThrow("offline"); });
    expect(result.current.liveView?.launch.intent).toBe("second");
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("ignores a late old-owner reload when the conversation changes", async () => {
    const { result, rerender } = renderHook(({ cid }) => useLiveBrowserSession(cid), { initialProps: { cid: "old" } });
    await waitFor(() => expect(result.current.liveView?.launch).toBe(first));
    let resolve!: (value: PreviewLaunch) => void;
    mint.mockReturnValueOnce(new Promise<PreviewLaunch>((r) => { resolve = r; }));
    let pending!: Promise<void>;
    act(() => { pending = result.current.reloadPreview(); });
    ready.mockResolvedValue({ ready: false });
    rerender({ cid: "new" });
    await act(async () => { resolve({ ...first, intent: "stale" }); await pending; });
    expect(result.current.liveView).toBeNull();
    expect(stop).toHaveBeenCalledWith("old");
    expect(start).toHaveBeenCalledTimes(1);
  });
});
