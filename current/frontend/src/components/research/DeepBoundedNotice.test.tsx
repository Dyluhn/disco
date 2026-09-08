/**
 * The honest-disclosure aside. Three things can be left open when a report is
 * published: the research budget that ended the evidence loop, a sentence the
 * grounding verifier could not support against the evidence, and a model
 * review that never returned a verdict. All are declared here — a report that
 * shipped with an unverified sentence must never look identical to one that
 * closed everything.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReportEvent } from "@/types/agent";
import { DeepBoundedNotice, hasReportDisclosure } from "./DeepBoundedNotice";

function makeReport(overrides: Partial<ReportEvent> = {}): ReportEvent {
  return {
    id: "e1",
    kind: "report",
    query: "the state of the subject",
    summary: "A grounded summary.",
    sections: [],
    passages: [],
    all_hits: [],
    unsupported_count: 0,
    bounded_by: null,
    depth_tier: "standard_deep",
    ...overrides,
  };
}

const SENTENCE = "The central finding holds across every measured deployment [[f1161a_p2]].";
const SENTENCE_AS_PROSE = "The central finding holds across every measured deployment.";

describe("DeepBoundedNotice", () => {
  it("does not describe source inspection turns as new web searches", () => {
    render(<DeepBoundedNotice report={makeReport({ bounded_by: "turns", meta: {
      turn_accounting: { model_turns: 8, degraded_turns: 0, inspection_turns: 2, of: 8 },
    } })} />);
    expect(screen.getByText(/2 turns requested a closer reading of already gathered sources/)).toBeInTheDocument();
    expect(screen.queryByText(/every one went to a search/)).not.toBeInTheDocument();
  });

  it("discloses unresolved checks even when the legacy unsupported count is zero", () => {
    const report = makeReport({ meta: { grounding_counts: {
      supported: 8, contradicted: 0, unresolved: 3, unavailable: 2,
    } } });
    render(<DeepBoundedNotice report={report} />);
    expect(hasReportDisclosure(report)).toBe(true);
    expect(screen.getByText(/8 supported, 0 possible contradictions, 3 unresolved, 2 not checked/)).toBeInTheDocument();
  });

  it("keeps an unreturned coverage repair visible outside the report", () => {
    const note = "Requested coverage was not returned: Network filesystem limitations.";
    const report = makeReport({ meta: { review_notes: [note, null, ""] } });
    render(<DeepBoundedNotice report={report} />);
    expect(hasReportDisclosure(report)).toBe(true);
    expect(screen.getByText(note)).toBeInTheDocument();
  });

  it("does not invent counts from malformed metadata", () => {
    const report = makeReport({ meta: { grounding_counts: {
      supported: 8, contradicted: -1, unresolved: "3", unavailable: 0,
    }, review_notes: "not a list" } });
    const { container } = render(<DeepBoundedNotice report={report} />);
    expect(container.firstChild).toBeNull();
    expect(hasReportDisclosure(report)).toBe(false);
  });

  it("renders nothing when the run closed everything", () => {
    const report = makeReport();
    const { container } = render(<DeepBoundedNotice report={report} />);
    expect(container.firstChild).toBeNull();
    expect(hasReportDisclosure(report)).toBe(false);
  });

  it("stays silent for the depth-only 'rounds' cap", () => {
    const report = makeReport({ bounded_by: "rounds" });
    const { container } = render(<DeepBoundedNotice report={report} />);
    expect(container.firstChild).toBeNull();
    expect(hasReportDisclosure(report)).toBe(false);
  });

  it("names the research budget that closed the evidence loop", () => {
    render(<DeepBoundedNotice report={makeReport({ bounded_by: "sources" })} />);
    expect(screen.getByText(/the web-source budget/)).toBeInTheDocument();
  });

  it("declares a sentence the verifier could not ground, as prose", () => {
    const report = makeReport({ meta: { unverified_sentences: [SENTENCE] } });
    render(<DeepBoundedNotice report={report} />);

    expect(hasReportDisclosure(report)).toBe(true);
    expect(
      screen.getByText(/One sentence could not be verified against the gathered evidence/),
    ).toBeInTheDocument();
    // The stored sentence keeps its citation marker; the aside drops the markup only.
    expect(screen.getByText(SENTENCE_AS_PROSE)).toBeInTheDocument();
    expect(screen.queryByText(/\[\[/)).not.toBeInTheDocument();
  });

  it("counts multiple unverified sentences", () => {
    const second = "A second claim the evidence does not carry.";
    render(
      <DeepBoundedNotice
        report={makeReport({ meta: { unverified_sentences: [SENTENCE, second] } })}
      />,
    );
    expect(
      screen.getByText(/2 sentences could not be verified against the gathered evidence/),
    ).toBeInTheDocument();
    expect(screen.getByText(second)).toBeInTheDocument();
  });

  it("still reads a report persisted with the legacy residual_deficiencies key", () => {
    const legacy = 'R3 — at "the central finding": Add a [[id]] citation to this sentence';
    const report = makeReport({ meta: { residual_deficiencies: [legacy] } });
    render(<DeepBoundedNotice report={report} />);
    expect(hasReportDisclosure(report)).toBe(true);
    expect(screen.getByText(/One sentence could not be verified/)).toBeInTheDocument();
  });

  it("says when the model review never returned a verdict", () => {
    const report = makeReport({ meta: { review_outcome: "unavailable" } });
    render(<DeepBoundedNotice report={report} />);
    expect(hasReportDisclosure(report)).toBe(true);
    expect(
      screen.getByText(/The model's editorial review was unavailable for this run/),
    ).toBeInTheDocument();
    // Not a budget bound — no bigger-tier offer, no bound sentence.
    expect(screen.queryByText(/This run was bounded by/)).not.toBeInTheDocument();
  });

  it("stays silent for a review that returned its verdict", () => {
    const report = makeReport({ meta: { review_outcome: "verdict" } });
    const { container } = render(<DeepBoundedNotice report={report} />);
    expect(container.firstChild).toBeNull();
    expect(hasReportDisclosure(report)).toBe(false);
  });

  it("discloses an incomplete review even when no other notice was stored", () => {
    const report = makeReport({ meta: { review_outcome: "incomplete" } });
    render(<DeepBoundedNotice report={report} />);
    expect(hasReportDisclosure(report)).toBe(true);
    expect(screen.getByText(/Review found unresolved issues/)).toBeInTheDocument();
  });

  it("offers the bigger research budget only for a BUDGET bound", () => {
    const onTryExhaustive = vi.fn();
    const { rerender } = render(
      <DeepBoundedNotice
        report={makeReport({ meta: { unverified_sentences: [SENTENCE] } })}
        onTryExhaustive={onTryExhaustive}
      />,
    );
    // A rerun cannot verify a sentence the evidence does not carry — no false affordance.
    expect(screen.queryByText("Run on Exhaustive tier")).not.toBeInTheDocument();

    rerender(
      <DeepBoundedNotice
        report={makeReport({ bounded_by: "turns" })}
        onTryExhaustive={onTryExhaustive}
      />,
    );
    expect(screen.getByText("Run on Exhaustive tier")).toBeInTheDocument();
  });

  it("distinguishes turns the model spent from turns an infrastructure outage ate", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "turns",
          meta: { turn_accounting: { model_turns: 5, degraded_turns: 11, of: 16 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        /The run recorded 16 research turns of its 16; 11 of those came back empty because searching or reading the pages failed/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/while the search pool is healthy/)).toBeInTheDocument();
  });

  it("says so plainly when every turn was productive", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "turns",
          meta: { turn_accounting: { model_turns: 16, degraded_turns: 0, of: 16 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        "The run recorded 16 research turns of its 16, and every one went to a search that came back with results.",
      ),
    ).toBeInTheDocument();
  });

  // S1's `turn-accounting-mismatch-evidence.json`: a REAL finished report whose
  // ledger reads {model_turns: 7, degraded_turns: 0, of: 8}. The notice used to
  // print "All 8 research turns …" — the budget, not what the engine recorded.
  it("reports turns SPENT, not the budget, when the two differ", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "sources",
          meta: { turn_accounting: { model_turns: 7, degraded_turns: 0, of: 8 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        "The run recorded 7 research turns of its 8, and every one went to a search that came back with results.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/All 8 research turns/)).not.toBeInTheDocument();
  });

  // F6 renders "Not searched ×3" in the trace for turns whose every proposed
  // query the host's walls refused. The notice a few lines below claimed the
  // opposite, because those turns are in `model_turns` and `degraded_turns` was 0.
  it("says a turn issued no search when every query on it had already been run", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "turns",
          meta: { turn_accounting: { model_turns: 16, degraded_turns: 0, refused_turns: 3, of: 16 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        "The run recorded 16 research turns of its 16. 3 turns issued no search at all: every query proposed on them had already been run.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/every one went to a search that came back with results/),
    ).not.toBeInTheDocument();
  });

  it("names refused turns alongside turns an infrastructure outage ate", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "turns",
          meta: { turn_accounting: { model_turns: 5, degraded_turns: 11, refused_turns: 1, of: 16 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        /11 of those came back empty because searching or reading the pages failed, not because the subject ran out\. 1 turn issued no search at all: every query proposed on it had already been run\./,
      ),
    ).toBeInTheDocument();
  });

  it("renders a report written before refused_turns existed exactly as before", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({
          bounded_by: "turns",
          meta: { turn_accounting: { model_turns: 16, degraded_turns: 0, of: 16 } },
        })}
      />,
    );
    expect(
      screen.getByText(
        "The run recorded 16 research turns of its 16, and every one went to a search that came back with results.",
      ),
    ).toBeInTheDocument();
  });

  it("falls back to the generic bound rather than guessing when no ledger was recorded", () => {
    render(<DeepBoundedNotice report={makeReport({ bounded_by: "turns" })} />);
    expect(screen.getByText(/the research-turn budget/)).toBeInTheDocument();
    expect(screen.queryByText(/searching or reading the pages failed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/research turns went to searches/)).not.toBeInTheDocument();
  });

  it("ignores a malformed turn_accounting payload", () => {
    render(
      <DeepBoundedNotice
        report={makeReport({ bounded_by: "turns", meta: { turn_accounting: "not a ledger" } })}
      />,
    );
    expect(screen.queryByText(/searching or reading the pages failed/)).not.toBeInTheDocument();
  });

  it("ignores a malformed unverified_sentences payload", () => {
    const report = makeReport({ meta: { unverified_sentences: "not a list" } });
    const { container } = render(<DeepBoundedNotice report={report} />);
    expect(container.firstChild).toBeNull();
  });
});
