/**
 * BP-11: uploadFiles API function — unit tests.
 *
 * Verifies that uploadFiles POSTs FormData (not JSON) and returns the
 * server's saved/rejected structure. Network is mocked via vi.stubGlobal("fetch").
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { uploadFiles } from "@/api/agent";
import * as clientModule from "@/api/client";

// ---- helpers ----------------------------------------------------------------

function makeFetchStub(body: object, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

// ---- tests ------------------------------------------------------------------

describe("uploadFiles", () => {
  // Activate live mode so the function hits fetch, not the offline fixture.
  beforeEach(() => {
    vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
    vi.spyOn(clientModule, "agentHttpBase").mockReturnValue("http://agent");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("posts FormData (not JSON) to /conversations/{cid}/files", async () => {
    const stub = makeFetchStub({ saved: [], rejected: [] });
    vi.stubGlobal("fetch", stub);

    const file = new File(["hello"], "test.csv", { type: "text/csv" });
    await uploadFiles("conv_123", [file]);

    expect(stub).toHaveBeenCalledOnce();
    const [url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://agent/conversations/conv_123/files");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    // Content-Type must NOT be set (let the browser add the multipart boundary)
    expect((init.headers as Record<string, string> | undefined)?.["content-type"]).toBeUndefined();
  });

  it("appends each File under the 'files' field", async () => {
    const stub = makeFetchStub({ saved: [], rejected: [] });
    vi.stubGlobal("fetch", stub);

    const f1 = new File(["a"], "a.csv");
    const f2 = new File(["b"], "b.csv");
    await uploadFiles("conv_x", [f1, f2]);

    const fd = stub.mock.calls[0][1].body as FormData;
    const all = fd.getAll("files");
    expect(all).toHaveLength(2);
    expect((all[0] as File).name).toBe("a.csv");
    expect((all[1] as File).name).toBe("b.csv");
  });

  it("returns the saved/rejected payload", async () => {
    const payload = {
      saved: [{ name: "data.csv", bytes: 42 }],
      rejected: [{ name: "big.bin", reason: "file exceeds 25 MB limit" }],
    };
    vi.stubGlobal("fetch", makeFetchStub(payload));

    const result = await uploadFiles("conv_y", [new File([], "x")]);
    expect(result.saved).toEqual(payload.saved);
    expect(result.rejected).toEqual(payload.rejected);
  });

  it("returns empty arrays in offline mode (agentLive = false)", async () => {
    vi.spyOn(clientModule, "agentLive").mockReturnValue(false);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await uploadFiles("conv_z", [new File([], "f")]);
    expect(result).toEqual({ saved: [], rejected: [] });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns the 413 payload without throwing (partial rejection)", async () => {
    const payload = {
      saved: [{ name: "ok.txt", bytes: 5 }],
      rejected: [{ name: "big.bin", reason: "file exceeds 25 MB limit" }],
    };
    vi.stubGlobal("fetch", makeFetchStub(payload, 413));

    const result = await uploadFiles("conv_partial", [new File([], "x")]);
    expect(result.saved).toHaveLength(1);
    expect(result.rejected).toHaveLength(1);
  });
});
