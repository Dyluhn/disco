/**
 * C1: exportReport URL fix — the POST must go to the agent-server base, not
 * the bare page origin. Mirrors the agent.upload.test.ts pattern: mock fetch
 * via vi.stubGlobal and spy on agentHttpBase so we can assert the full URL.
 *
 * WALK-03 (C1): serializeReportToMarkdown must strip [[passage_id]] markers
 * from disputed_notes so they don't leak into plain-text exports.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  serializeReportToMarkdown,
} from "@/api/deepResearch";
import type { ReportEvent } from "@/types/agent";
import type { exportReport as exportReportFn } from "@/api/deepResearch";

// ---- helpers ----------------------------------------------------------------

type ExportReport = typeof exportReportFn;

function jsonResponse(body: object, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

function makeFetchStub(status = 200, body: BodyInit = new Blob(["data"], { type: "application/pdf" })) {
  return vi.fn(async (url: RequestInfo | URL) => {
    if (String(url) === "http://agent:8123/api/auth/session") {
      return jsonResponse({ authenticated: true, csrf_token: "csrf-token" });
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(),
      blob: () => Promise.resolve(body instanceof Blob ? body : new Blob([body])),
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(""),
    } as unknown as Response;
  });
}

async function importLiveDeepResearch(): Promise<{ exportReport: ExportReport }> {
  vi.resetModules();
  vi.stubGlobal("__DISCO_ENV", { AGENT_BASE: "http://agent:8123" });
  return import("@/api/deepResearch");
}

async function importDeepResearchWithMockedLiveCreate() {
  vi.resetModules();
  vi.unstubAllGlobals();
  const clientModule = await import("@/api/client");
  vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
  const send = vi
    .spyOn(clientModule, "agentSend")
    .mockResolvedValue({ conversation_id: "conv_iter" } as never);
  const { createDeepResearchConversation } = await import("@/api/deepResearch");
  return { createDeepResearchConversation, send };
}

function exportCall(stub: ReturnType<typeof vi.fn>) {
  const call = stub.mock.calls.find(([url]) =>
    String(url).includes("/report/export"),
  );
  if (!call) throw new Error("report export endpoint was not called");
  return call as [string, RequestInit];
}

// ---- tests ------------------------------------------------------------------

describe("exportReport", () => {
  beforeEach(() => {
    // jsdom has no URL.createObjectURL — stub it so downloadBlob doesn't throw
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn().mockReturnValue("blob:fake"),
      revokeObjectURL: vi.fn(),
    });
    // document.body.appendChild / anchor.click are available in jsdom; no stub needed.
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs to agent-server base, not bare /api (pdf)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    const stub = makeFetchStub(200);
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_abc", "pdf");

    const [url, init] = exportCall(stub);
    expect(url).toBe("http://agent:8123/api/conversations/conv_abc/report/export?fmt=pdf");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("x-disco-csrf")).toBe("csrf-token");
  });

  it("POSTs to agent-server base, not bare /api (pdf, no docx)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    // W-12: docx export was removed; pdf still routes to the agent-server base.
    const stub = makeFetchStub(200, new Blob(["data"], { type: "application/pdf" }));
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_xyz", "pdf");

    const [url] = exportCall(stub);
    expect(url).toBe("http://agent:8123/api/conversations/conv_xyz/report/export?fmt=pdf");
    // URL must NOT be a bare relative path
    expect(url).not.toMatch(/^\/api\//);
  });

  it("throws on non-ok response with detail from JSON body", async () => {
    const { exportReport } = await importLiveDeepResearch();
    const stub = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url) === "http://agent:8123/api/auth/session") {
        return jsonResponse({ authenticated: true, csrf_token: "csrf-token" });
      }
      return {
        ok: false,
        status: 503,
        headers: new Headers(),
        blob: () => Promise.resolve(new Blob()),
        json: () => Promise.resolve({ detail: { reason: "pandoc unavailable" } }),
        text: () => Promise.resolve(""),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_fail", "pdf")).rejects.toThrow("Export failed (503): pandoc unavailable");
  });

  it("throws when fmt is md (client-side path should be used instead)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    // md is gated before fetch — no network call should be made
    const stub = vi.fn();
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_any", "md")).rejects.toThrow(/exportReportAsMarkdown/);
    expect(stub).not.toHaveBeenCalled();
  });
});

// ---- A4: createDeepResearchConversation threads `iterative` into the create frame ----

describe("createDeepResearchConversation — A4 iterative grounding", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends iterative:true when the toggle is ON (mirrors depth_tier flow)", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({ query: "q", iterative: true });

    const [, path, body] = send.mock.calls[0] as [string, string, Record<string, unknown>];
    expect(path).toBe("/conversations");
    expect(body.surface).toBe("deep_research");
    expect(body.iterative).toBe(true);
  });

  it("defaults iterative:false when the toggle is omitted (byte-identical OFF)", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({ query: "q" });

    const [, , body] = send.mock.calls[0] as [string, string, Record<string, unknown>];
    expect(body.iterative).toBe(false);
    // and it still carries the depth_tier default — proving we mirror, not replace
    expect(body.depth_tier).toBe("standard_deep");
  });

  it("sends sources when per-query sources are selected", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({ query: "q", sources: ["arxiv", "ddgs"] });

    const [, , body] = send.mock.calls[0] as [string, string, Record<string, unknown>];
    expect(body.sources).toEqual(["arxiv", "ddgs"]);
  });
});

// ---- WALK-03 (C1): serializeReportToMarkdown strips [[id]] from disputed_notes ----

function makeMinimalReport(overrides: Partial<ReportEvent> = {}): ReportEvent {
  return {
    id: "r1",
    kind: "report",
    seq: 10,
    query: "test query",
    summary: "Summary",
    sections: [],
    passages: [],
    all_hits: [],
    unsupported_count: 0,
    bounded_by: null,
    depth_tier: "standard_deep",
    ...overrides,
  };
}

describe("serializeReportToMarkdown — WALK-03 disputed_notes [[id]] stripping", () => {
  it("WALK-03: strips [[passage_id]] markers from disputed_notes in the export", () => {
    const report = makeMinimalReport({
      sections: [
        {
          id: "s1",
          title: "Section 1",
          markdown: "Body text.",
          cited_passage_ids: [],
          confidence: "mixed",
          disputed_notes: [
            "One source argues X [[bce679_p0]].",
            "Another claims Y [[abc123]].",
          ],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);

    // The raw [[id]] markers must not appear in the export
    expect(md).not.toMatch(/\[\[bce679_p0\]\]/);
    expect(md).not.toMatch(/\[\[abc123\]\]/);
    // But the surrounding prose should still be present
    expect(md).toContain("One source argues X");
    expect(md).toContain("Another claims Y");
    // The conflicts line itself should appear
    expect(md).toContain("_Conflicts noted:");
  });

  it("WALK-03: a note that is ONLY a [[id]] marker (no surrounding text) is dropped", () => {
    const report = makeMinimalReport({
      sections: [
        {
          id: "s1",
          title: "Section 1",
          markdown: "Body.",
          cited_passage_ids: [],
          confidence: "mixed",
          disputed_notes: ["[[id_only]]"],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    // After stripping, the note is empty → the whole conflicts line is dropped
    expect(md).not.toContain("_Conflicts noted:");
    expect(md).not.toMatch(/\[\[id_only\]\]/);
  });

  it("WALK-03: sections with no disputed_notes still export cleanly", () => {
    const report = makeMinimalReport({
      sections: [
        {
          id: "s1",
          title: "Section 1",
          markdown: "Normal section.",
          cited_passage_ids: [],
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    expect(md).toContain("Normal section.");
    expect(md).not.toContain("_Conflicts noted:");
  });
});
