/**
 * C1: exportReport URL fix — the POST must go to the agent-server base, not
 * the bare page origin. Mirrors the agent.upload.test.ts pattern: mock fetch
 * via vi.stubGlobal and spy on agentHttpBase so we can assert the full URL.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { exportReport } from "@/api/deepResearch";
import * as clientModule from "@/api/client";

// ---- helpers ----------------------------------------------------------------

function makeFetchStub(status = 200, body: BodyInit = new Blob(["data"], { type: "application/pdf" })) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    blob: () => Promise.resolve(body instanceof Blob ? body : new Blob([body])),
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(""),
  });
}

// ---- tests ------------------------------------------------------------------

describe("exportReport", () => {
  beforeEach(() => {
    vi.spyOn(clientModule, "agentHttpBase").mockReturnValue("http://agent:8123");
    // jsdom has no URL.createObjectURL — stub it so downloadBlob doesn't throw
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn().mockReturnValue("blob:fake"),
      revokeObjectURL: vi.fn(),
    });
    // document.body.appendChild / anchor.click are available in jsdom; no stub needed.
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("POSTs to agent-server base, not bare /api (pdf)", async () => {
    const stub = makeFetchStub(200);
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_abc", "pdf");

    expect(stub).toHaveBeenCalledOnce();
    const [url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://agent:8123/api/conversations/conv_abc/report/export?fmt=pdf");
    expect(init.method).toBe("POST");
  });

  it("POSTs to agent-server base, not bare /api (docx)", async () => {
    const stub = makeFetchStub(200, new Blob(["data"], { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" }));
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_xyz", "docx");

    expect(stub).toHaveBeenCalledOnce();
    const [url] = stub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://agent:8123/api/conversations/conv_xyz/report/export?fmt=docx");
    // URL must NOT be a bare relative path
    expect(url).not.toMatch(/^\/api\//);
  });

  it("throws on non-ok response with detail from JSON body", async () => {
    const stub = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      blob: () => Promise.resolve(new Blob()),
      json: () => Promise.resolve({ detail: { reason: "pandoc unavailable" } }),
      text: () => Promise.resolve(""),
    });
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_fail", "pdf")).rejects.toThrow("Export failed (503): pandoc unavailable");
  });

  it("throws when fmt is md (client-side path should be used instead)", async () => {
    // md is gated before fetch — no network call should be made
    const stub = vi.fn();
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_any", "md")).rejects.toThrow(/exportReportAsMarkdown/);
    expect(stub).not.toHaveBeenCalled();
  });
});
