import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { usePreviewLaunch } from "./usePreviewLaunch";
import type { PreviewLaunch } from "@/api/preview";

const { mint } = vi.hoisted(() => ({ mint: vi.fn() }));
vi.mock("@/api/preview", () => ({ previewBootstrapUrl: mint, previewErrorReason: () => null }));
const first = { url: "http://preview/bootstrap", intent: "first", accessDeadline: 1 };
function deferred() {
  let resolve!: (launch: PreviewLaunch) => void;
  const promise = new Promise<PreviewLaunch>((r) => { resolve = r; });
  return { promise, resolve };
}
beforeEach(() => { mint.mockReset(); mint.mockResolvedValue(first); });

describe("explicit preview access reload", () => {
  it("mints one fresh launch while retaining the old page throughout a pending or failed request", async () => {
    const { result } = renderHook(() => usePreviewLaunch("cid", true, "one", "/typed", null));
    await waitFor(() => expect(result.current.launch).toBe(first));
    const pending = deferred();
    mint.mockReturnValueOnce(pending.promise);
    let reload!: Promise<void>;
    act(() => { reload = result.current.reloadPreview(); });
    expect(result.current.launch).toBe(first);
    expect(result.current.minting).toBe(true);
    await act(async () => { pending.resolve({ ...first, intent: "second" }); await reload; });
    expect(mint).toHaveBeenCalledTimes(2);
    expect(mint).toHaveBeenLastCalledWith("cid", "/typed", null);
    const retained = result.current.launch;
    mint.mockRejectedValueOnce(new Error("offline"));
    await act(async () => { await expect(result.current.reloadPreview()).rejects.toThrow("offline"); });
    expect(result.current.launch).toBe(retained);
    expect(result.current.launchFailure).not.toBeNull();
    expect(result.current.minting).toBe(false);
  });

  it("ignores an old conversation's late explicit reload after selection changes", async () => {
    const { result, rerender } = renderHook(({ cid }) => usePreviewLaunch(cid, true, cid, "/", null),
      { initialProps: { cid: "old" } });
    await waitFor(() => expect(result.current.launch).toBe(first));
    const pending = deferred();
    mint.mockReturnValueOnce(pending.promise);
    let reload!: Promise<void>;
    act(() => { reload = result.current.reloadPreview(); });
    mint.mockResolvedValueOnce({ ...first, intent: "new-owner" });
    rerender({ cid: "new" });
    await waitFor(() => expect(result.current.launch?.intent).toBe("new-owner"));
    await act(async () => { pending.resolve({ ...first, intent: "stale-owner" }); await reload; });
    expect(result.current.launch?.intent).toBe("new-owner");
  });

  it("does not let an earlier navigation mint replace a completed explicit reload", async () => {
    const oldMint = deferred();
    mint.mockReturnValueOnce(oldMint.promise);
    const { result } = renderHook(() => usePreviewLaunch("cid", true, "one", "/", null));
    mint.mockResolvedValueOnce({ ...first, intent: "confirmed-reload" });
    await act(async () => { await result.current.reloadPreview(); });
    await act(async () => { oldMint.resolve(first); await oldMint.promise; });
    expect(result.current.launch?.intent).toBe("confirmed-reload");
    expect(result.current.minting).toBe(false);
  });
});
