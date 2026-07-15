/** Citation numbering ↔ Sources panel alignment (the 2026-07-09 off-by-one).
 *
 * The regression shape is the REAL captured report from the fresh-install
 * walkthrough (conv_6f529a79, "Latest US Iran Conflict News"): 6 passages from
 * 5 sources — the nbcnews live-blog contributed TWO passages (f712fc_p2 +
 * f712fc_p8). The old chip numbering used the raw passage index, so every chip
 * after the duplicate drifted +1 from the deduped Sources rows (chip [5] for
 * sources-row [4]). Chips must share their SOURCE's first-seen number.
 */

import { describe, expect, it } from "vitest";
import type { GroundedAnswer } from "@/types/grounded";
import { deriveSourceTiers } from "./deepResearchTrace";
import type { ReportEvent } from "@/types/agent";
import { citationNumbers } from "./sources";

// Verbatim shape (ids + urls) of the captured duplicate-source report.
const PASSAGES = [
  { id: "7d933c_p11", source_url: "https://en.wikipedia.org/wiki/2026_Iran_war", source_title: "w", text: "t" },
  { id: "64b8fe_p1", source_url: "https://www.cnn.com/2026/07/09/iran-us-ceasefire", source_title: "c", text: "t" },
  { id: "f712fc_p2", source_url: "https://www.nbcnews.com/world/iran/live-blog", source_title: "n", text: "t" },
  { id: "f712fc_p8", source_url: "https://www.nbcnews.com/world/iran/live-blog", source_title: "n", text: "t" },
  { id: "3690ed_p0", source_url: "https://www.nbcnews.com/world/iran/us-iran-cycle", source_title: "n2", text: "t" },
  { id: "e74a5d_p10", source_url: "https://quincyinst.org/research/containment", source_title: "q", text: "t" },
];

const answer = { passages: PASSAGES, claims: [] } as unknown as GroundedAnswer;
const report = { passages: PASSAGES, all_hits: [] } as unknown as ReportEvent;

describe("citationNumbers (source-position numbering)", () => {
  it("gives passages from the same source the SAME number", () => {
    const n = citationNumbers(answer);
    expect(n.get("f712fc_p2")).toBe(3);
    expect(n.get("f712fc_p8")).toBe(3); // duplicate source → shared chip number
  });

  it("does not drift after a duplicate (the reported off-by-one)", () => {
    const n = citationNumbers(answer);
    expect(n.get("3690ed_p0")).toBe(4); // was 5 under raw-index numbering
    expect(n.get("e74a5d_p10")).toBe(5); // was 6
  });

  it("numbers every chip 1..#sources, never past the sources list", () => {
    const n = citationNumbers(answer);
    const max = Math.max(...[...n.values()]);
    expect(max).toBe(5); // 5 unique sources, 6 passages
  });

  it("gives a URL-less passage its own number instead of [0]", () => {
    const weird = {
      passages: [
        { id: "a_p0", source_url: "https://x.example/a", source_title: "", text: "" },
        { id: "orphan", source_url: "", source_title: "", text: "" },
      ],
      claims: [],
    } as unknown as GroundedAnswer;
    const n = citationNumbers(weird);
    expect(n.get("orphan")).toBe(2);
  });
});

describe("chips ↔ Sources panel alignment (the binding invariant)", () => {
  it("chip number == 1-based index of the passage's row in the deduped cited tier", () => {
    const n = citationNumbers(answer);
    const { cited } = deriveSourceTiers(report);
    expect(cited).toHaveLength(5);
    for (const p of PASSAGES) {
      const row = cited.findIndex((r) => String(r.source_url) === p.source_url);
      expect(row).toBeGreaterThanOrEqual(0);
      expect(n.get(p.id)).toBe(row + 1);
    }
  });
});
