/**
 * C1: exportReport URL fix — the POST must go to the agent-server base, not
 * the bare page origin. Mirrors the agent.upload.test.ts pattern: mock fetch
 * via vi.stubGlobal and spy on agentHttpBase so we can assert the full URL.
 *
 * WALK-03 (C1): serializeReportToMarkdown must strip [[passage_id]] markers
 * from disputed_notes so they don't leak into plain-text exports.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { exportReport, serializeReportToMarkdown } from "@/api/deepResearch";
import * as clientModule from "@/api/client";
import type { ReportEvent } from "@/types/agent";

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
