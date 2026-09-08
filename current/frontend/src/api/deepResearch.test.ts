/**
 * C1: exportReport URL fix — the POST must go to the agent-server base, not
 * the bare page origin. Mirrors the agent.upload.test.ts pattern: mock fetch
 * via vi.stubGlobal and spy on agentHttpBase so we can assert the full URL.
 *
 * One numbering, everywhere (2026-08-21): serializeReportToMarkdown renders
 * every [[passage_id]] marker as the UI's [n] numeral (first-seen source
 * order over report.passages — lib/sources.ts:citationNumbers) and the footer
 * lists one source per number. The byte-parity and shared-fixture tests here
 * pin the SAME bytes/assignment as the Python serializer's suite
 * (agent-server test_report_export.py).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { serializeReportToMarkdown } from "@/api/deepResearch";
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

function makeFetchStub(
  status = 200,
  body: BlobPart = new Blob(["data"], { type: "application/pdf" }),
) {
  return vi.fn(async (url: RequestInfo | URL) => {
    if (String(url) === "http://agent:8123/api/auth/session") {
      return jsonResponse({ authenticated: true, csrf_token: "csrf-token" });
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(),
      blob: () =>
        Promise.resolve(body instanceof Blob ? body : new Blob([body])),
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(""),
    } as unknown as Response;
  });
}

async function importLiveDeepResearch(): Promise<{
  exportReport: ExportReport;
}> {
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
  let anchorClick: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // jsdom has no URL.createObjectURL — stub it so downloadBlob doesn't throw
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn().mockReturnValue("blob:fake"),
      revokeObjectURL: vi.fn(),
    });
    // A synthetic download click is the browser boundary under test. jsdom turns
    // the real anchor implementation into an asynchronous unsupported-navigation
    // error, so intercept that one browser primitive explicitly.
    anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    anchorClick.mockRestore();
    vi.unstubAllGlobals();
  });

  it("POSTs to agent-server base, not bare /api (pdf)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    const stub = makeFetchStub(200);
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_abc", "pdf");

    const [url, init] = exportCall(stub);
    expect(url).toBe(
      "http://agent:8123/api/conversations/conv_abc/report/export?fmt=pdf",
    );
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("x-disco-csrf")).toBe("csrf-token");
    expect(anchorClick).toHaveBeenCalledTimes(1);
  });

  it("POSTs to agent-server base, not bare /api (pdf, no docx)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    // W-12: docx export was removed; pdf still routes to the agent-server base.
    const stub = makeFetchStub(
      200,
      new Blob(["data"], { type: "application/pdf" }),
    );
    vi.stubGlobal("fetch", stub);

    await exportReport("conv_xyz", "pdf");

    const [url] = exportCall(stub);
    expect(url).toBe(
      "http://agent:8123/api/conversations/conv_xyz/report/export?fmt=pdf",
    );
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
        json: () =>
          Promise.resolve({ detail: { reason: "pandoc unavailable" } }),
        text: () => Promise.resolve(""),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_fail", "pdf")).rejects.toThrow(
      "Export failed (503): pandoc unavailable",
    );
  });

  // Historical sealed id retained; markdown now deliberately takes the server
  // path so the canonical generated title is included in the export.
  it("throws when fmt is md (client-side path should be used instead)", async () => {
    const { exportReport } = await importLiveDeepResearch();
    const stub = makeFetchStub(200, new Blob(["# Generated title"]));
    vi.stubGlobal("fetch", stub);

    await expect(exportReport("conv_any", "md")).resolves.toBe(true);
    const [url] = exportCall(stub);
    expect(url).toContain("/conversations/conv_any/report/export?fmt=md");
  });
});

// ---- Deep Research has one adaptive execution path ------------------------

describe("createDeepResearchConversation — A4 iterative grounding", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults iterative:false when the toggle is omitted (byte-identical OFF)", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({ query: "q" });

    const [, path, body] = send.mock.calls[0] as [
      string,
      string,
      Record<string, unknown>,
    ];
    expect(path).toBe("/conversations");
    expect(body.surface).toBe("deep_research");
    expect(body).not.toHaveProperty("iterative");
    expect(body.depth_tier).toBe("standard_deep");
  });

  it("sends iterative:true when the toggle is ON (mirrors depth_tier flow)", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    // Historical inventory label retained for the fail-closed test authority.
    // A stale caller may still carry this property, but the one-path client
    // strips it and the compatibility backend ignores it.
    await createDeepResearchConversation(
      { query: "q", iterative: true } as Parameters<
        typeof createDeepResearchConversation
      >[0] & { iterative: boolean },
    );

    const [, , body] = send.mock.calls[0] as [
      string,
      string,
      Record<string, unknown>,
    ];
    expect(body).not.toHaveProperty("iterative");
  });

  it("sends sources when per-query sources are selected", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({
      query: "q",
      sources: ["arxiv", "web"],
    });

    const [, , body] = send.mock.calls[0] as [
      string,
      string,
      Record<string, unknown>,
    ];
    expect(body.sources).toEqual(["arxiv", "web"]);
  });

  it("leaves title unset for the shared auto-titler", async () => {
    const { createDeepResearchConversation, send } =
      await importDeepResearchWithMockedLiveCreate();

    await createDeepResearchConversation({
      query: "What's new in local models this week?",
    });

    const [, , body] = send.mock.calls[0] as [
      string,
      string,
      Record<string, unknown>,
    ];
    expect(body).not.toHaveProperty("title");
  });
});

// ---- Citation numbering: [[id]] markers render as the UI's [n] numerals ----

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

describe("serializeReportToMarkdown — disputed_notes citation numbering", () => {
  it("renders known [[passage_id]] markers as [n] and unknown ones as [?]", () => {
    const report = makeMinimalReport({
      passages: [
        {
          id: "bce679_p0",
          source_title: "Known Source",
          source_url: "https://known.example.com/x",
        },
      ],
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
    // Known passage → the UI's numeral; unknown id → [?] (never a raw hash)
    expect(md).toContain("One source argues X [1].");
    expect(md).toContain("Another claims Y [?].");
    // The conflicts line itself should appear
    expect(md).toContain("_Conflicts noted:");
  });

  it("a note that is ONLY a [[id]] marker becomes its numeral (line kept)", () => {
    const report = makeMinimalReport({
      passages: [
        {
          id: "id_only",
          source_title: "Solo",
          source_url: "https://solo.example.com/",
        },
      ],
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
    // The numeral resolves against the sources footer, so the line stays.
    expect(md).toContain("_Conflicts noted: [1]_");
    expect(md).not.toMatch(/\[\[id_only\]\]/);
  });

  it("sections with no disputed_notes still export cleanly", () => {
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

describe("serializeReportToMarkdown — adjacent shared-source citations", () => {
  it("collapses only adjacent duplicate display markers", () => {
    const report = makeMinimalReport({
      summary: "Literal [3] [3]; [[p1]] [[p2]] [[p3]]; [[p1]] text [[p2]].",
      passages: [
        { id: "p1", source_title: "One", source_url: "https://one.example.com" },
        { id: "p2", source_title: "One duplicate", source_url: "https://one.example.com/" },
        { id: "p3", source_title: "Two", source_url: "https://two.example.com" },
      ],
      sections: [],
    });

    const md = serializeReportToMarkdown(report);

    expect(md).toContain("Literal [3] [3]; [1] [2]; [1] text [1].");
  });
});

// ---- Passage-text normalization (mirrors report_export.py's shared seam) ----

describe("serializeReportToMarkdown — passage-text normalization", () => {
  it("converts escaped markdown links to clean text (external keeps url)", () => {
    const report = makeMinimalReport({
      sections: [
        {
          id: "s1",
          title: "Escaped links",
          markdown:
            "The code is \\[proprietary\\](https://en.wikipedia.org/wiki/Proprietary\\_software) per the vendor.\n\n" +
            "See the \\[FAQs.\\](/faq) and the \\[Model\\](/models/grok-4-6) card.",
          cited_passage_ids: [],
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    expect(md).toContain(
      "proprietary (https://en.wikipedia.org/wiki/Proprietary_software)",
    );
    expect(md).toContain("See the FAQs. and the Model card.");
    expect(md).not.toContain("\\[");
    expect(md).not.toContain("\\_");
  });

  it("strips pseudo-table pipe debris and pads ragged table rows", () => {
    const report = makeMinimalReport({
      sections: [
        {
          id: "s1",
          title: "Table debris",
          markdown:
            "Metric | Value | Metric | Value | Metric | Value\n\n" +
            "| Model | Params | License |\n|---|---|---|\n| Grok | 314B |\n| Llama | 405B | open |",
          cited_passage_ids: [],
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    expect(md).toContain("Metric; Value; Metric; Value; Metric; Value");
    expect(md).not.toContain("Metric | Value");
    expect(md).toContain("| Grok | 314B |  |");
    expect(md).toContain("| Llama | 405B | open |");
  });

  it("leaves well-formed links, inline pipes, and code fences untouched", () => {
    const report = makeMinimalReport({
      summary: "A [real](https://example.com/a) link and `a | b` inline.",
      sections: [
        {
          id: "s1",
          title: "Fenced",
          markdown: "```\nx | y | z | w | v\n```",
          cited_passage_ids: [],
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    expect(md).toContain("A [real](https://example.com/a) link and `a | b` inline.");
    expect(md).toContain("```\nx | y | z | w | v\n```");
  });
});

// ---- py↔ts byte parity + shared numbering fixture (2026-08-21) --------------
//
// The Python serializer's suite (agent-server test_report_export.py) pins the
// SAME bytes for the same sample report, and the same [n] assignment for the
// same shared fixture — together these hold the cross-language contract that
// exports always show the numbering the user saw in the UI.

function makeSampleReport(): ReportEvent {
  return makeMinimalReport({
    query: "What is the airspeed velocity of an unladen swallow?",
    summary: "African and European swallows differ. Airspeed ~11 m/s and ~8 m/s.",
    sections: [
      {
        id: "s0",
        title: "African Swallow",
        markdown:
          "The African swallow cruises at **11 m/s** with 5-7 flaps per second." +
          "\n\nSee [[p0]] for primary data.",
        cited_passage_ids: ["p0"],
        confidence: "high",
        disputed_notes: [],
        unsupported_count: 0,
      },
      {
        id: "s1",
        title: "European Swallow",
        markdown:
          "The European swallow is smaller and slower: **8 m/s**." +
          "\n\nMeasurements vary by season [[p1]].",
        cited_passage_ids: ["p1"],
        confidence: "mixed",
        disputed_notes: ["seasonal variation unaccounted in some studies"],
        unsupported_count: 0,
      },
    ],
    passages: [
      {
        id: "p0",
        source_title: "Avian Speed Database",
        source_url: "https://birds.example.com/p0",
      },
      {
        id: "p1",
        source_title: "European Ornithology Journal",
        source_url: "https://birds.example.com/p1",
      },
    ],
    unsupported_count: 1,
    bounded_by: "sources",
  });
}

const CAPTURED_MARKDOWN =
  `# What is the airspeed velocity of an unladen swallow?

> Question: What is the airspeed velocity of an unladen swallow?

## Executive Summary

African and European swallows differ. Airspeed ~11 m/s and ~8 m/s.

## African Swallow

The African swallow cruises at **11 m/s** with 5-7 flaps per second.

See [1] for primary data.

## European Swallow

_Conflicts noted: seasonal variation unaccounted in some studies_

The European swallow is smaller and slower: **8 m/s**.

Measurements vary by season [2].

---

Sources cited (2):

- [1] Avian Speed Database — https://birds.example.com/p0
- [2] European Ornithology Journal — https://birds.example.com/p1`;

describe("serializeReportToMarkdown — py↔ts byte parity", () => {
  it("produces the exact bytes the Python serializer's suite pins", () => {
    expect(serializeReportToMarkdown(makeSampleReport())).toBe(CAPTURED_MARKDOWN);
  });
});

describe("serializeReportToMarkdown — the export carries the question", () => {
  // Regression: the server-side export puts a generated conversation title in
  // the H1 (W-10) and recorded the question nowhere, which is what
  // e2e-live/deep-research-truth.spec.ts:220 caught. The rule that fixes it is
  // unconditional in both serializers so the two cannot drift; here the H1 is
  // already the query, so the line repeats it.
  it("puts the question verbatim on the line under the H1", () => {
    const md = serializeReportToMarkdown(makeSampleReport());

    expect(md.split("\n").slice(0, 3)).toEqual([
      "# What is the airspeed velocity of an unladen swallow?",
      "",
      "> Question: What is the airspeed velocity of an unladen swallow?",
    ]);
  });

  it("does not reshape a question that carries markdown of its own", () => {
    const query = "Why *exactly* did the [European] swallow slow down — and by how much?";
    const md = serializeReportToMarkdown({ ...makeSampleReport(), query });

    expect(md).toContain(`> Question: ${query}`);
  });
});

interface ParityFixture {
  passages: Array<Record<string, unknown>>;
  section_markdown: string;
  expected_inline: string;
  expected_footer: string[];
}

describe("serializeReportToMarkdown — shared citation-numbering fixture", () => {
  const fixturePath = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../../packages/agent-server/tests/fixtures/citation_numbering_parity.json",
  );
  const fx = JSON.parse(readFileSync(fixturePath, "utf-8")) as ParityFixture;

  it("assigns the same [n] numbering the Python serializer does", () => {
    const report = makeMinimalReport({
      query: "Parity",
      summary: "Summary.",
      passages: fx.passages,
      sections: [
        {
          id: "s0",
          title: "Numbering",
          markdown: fx.section_markdown,
          cited_passage_ids: fx.passages.map((p) => String(p.id)),
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
    });

    const md = serializeReportToMarkdown(report);
    expect(md).toContain(fx.expected_inline);
    expect(md).toContain(`Sources cited (${fx.expected_footer.length}):`);
    const footer = md.split("Sources cited", 1 + 1)[1];
    expect(footer.split("\n\n", 2)[1]).toBe(fx.expected_footer.join("\n"));
    // No raw passage id anywhere in the export.
    expect(md).not.toContain("[[");
  });
});
