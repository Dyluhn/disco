/**
 * BP-11: uploadFiles API function — unit tests.
 *
 * Verifies that uploadFiles POSTs FormData (not JSON) and returns the
 * server's saved/rejected structure. Network is mocked via vi.stubGlobal("fetch").
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { uploadFiles as uploadFilesFn } from "@/api/agent";

// ---- helpers ----------------------------------------------------------------

type UploadFiles = typeof uploadFilesFn;

function jsonResponse(body: object, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

function makeFetchStub(body: object, status = 200) {
  return vi.fn(async (url: RequestInfo | URL) => {
    if (String(url) === "http://agent/api/auth/session") {
      return jsonResponse({ authenticated: true, csrf_token: "csrf-token" });
    }
    return jsonResponse(body, status);
  });
}

async function importLiveAgent(): Promise<{ uploadFiles: UploadFiles }> {
  vi.resetModules();
  vi.stubGlobal("__DISCO_ENV", { AGENT_BASE: "http://agent" });
  return import("@/api/agent");
}

async function importOfflineAgent(): Promise<{ uploadFiles: UploadFiles }> {
  vi.resetModules();
  vi.unstubAllGlobals();
  return import("@/api/agent");
}

function uploadCall(stub: ReturnType<typeof vi.fn>) {
  const call = stub.mock.calls.find(([url]) =>
    String(url).includes("/conversations/"),
  );
  if (!call) throw new Error("upload endpoint was not called");
  return call as [string, RequestInit];
}

// ---- tests ------------------------------------------------------------------

describe("uploadFiles", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts FormData (not JSON) to /conversations/{cid}/files", async () => {
    const { uploadFiles } = await importLiveAgent();
    const stub = makeFetchStub({ saved: [], rejected: [] });
    vi.stubGlobal("fetch", stub);

    const file = new File(["hello"], "test.csv", { type: "text/csv" });
    await uploadFiles("conv_123", [file]);

    const [url, init] = uploadCall(stub);
    expect(url).toBe("http://agent/conversations/conv_123/files");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("x-disco-csrf")).toBe("csrf-token");
    // Content-Type must NOT be set (let the browser add the multipart boundary)
    expect(new Headers(init.headers).get("content-type")).toBeNull();
  });

  it("appends each File under the 'files' field", async () => {
    const { uploadFiles } = await importLiveAgent();
    const stub = makeFetchStub({ saved: [], rejected: [] });
    vi.stubGlobal("fetch", stub);

    const f1 = new File(["a"], "a.csv");
    const f2 = new File(["b"], "b.csv");
    await uploadFiles("conv_x", [f1, f2]);

    const fd = uploadCall(stub)[1].body as FormData;
    const all = fd.getAll("files");
    expect(all).toHaveLength(2);
    expect((all[0] as File).name).toBe("a.csv");
    expect((all[1] as File).name).toBe("b.csv");
  });

  it("returns the saved/rejected payload", async () => {
    const { uploadFiles } = await importLiveAgent();
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
    const { uploadFiles } = await importOfflineAgent();
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await uploadFiles("conv_z", [new File([], "f")]);
    expect(result).toEqual({ saved: [], rejected: [] });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns the 413 payload without throwing (partial rejection)", async () => {
    const { uploadFiles } = await importLiveAgent();
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
