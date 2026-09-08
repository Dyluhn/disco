import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ClaimVerdicts } from "./ClaimVerdicts";
import type { Passage, VerifiedClaim } from "@/types/grounded";

const passages: Passage[] = [
  {
    id: "p0",
    source_url: "https://example.com/paper",
    source_title: "A very long source title that exceeds forty characters for truncation",
    text: "The paper text.",
  },
  {
    id: "p1",
    source_url: "https://example.com/blog",
    source_title: "Short blog",
    text: "Blog text.",
  },
];

const claims: VerifiedClaim[] = [
  {
    claim: { text: "Solid-state batteries are in mass production.", cited_passage_ids: ["p0"] },
    verdict: "unsupported",
    best_passage_id: "p0",
    entailment_score: 0.12,
  },
  {
    claim: { text: "Costs are competitive with Li-ion.", cited_passage_ids: ["p1"] },
    verdict: "weak",
    best_passage_id: "p1",
    entailment_score: 0.45,
  },
  {
    claim: { text: "Pilot plants exist.", cited_passage_ids: ["p0"] },
    verdict: "supported",
    best_passage_id: "p0",
    entailment_score: 0.91,
  },
];

describe("ClaimVerdicts", () => {
  it("does not invent a zero support estimate for missing evidence", () => {
    render(<ClaimVerdicts claims={[{ ...claims[0], entailment_score: 0,
      verification_status: "unresolved", verification_reason: "missing_evidence",
    }]} passages={[]} />);
    expect(screen.getByText("Cited evidence is missing.")).toBeInTheDocument();
    expect(screen.queryByText(/Support estimate/)).not.toBeInTheDocument();
  });

  it("distinguishes possible contradictions, unresolved evidence and checks that could not run", () => {
    const measured: VerifiedClaim[] = [
      { ...claims[0], verification_status: "contradicted", verification_reason: "contradiction" },
      { ...claims[1], verification_status: "unresolved", verification_reason: "conflicting_evidence" },
      { ...claims[2], verdict: "weak", entailment_score: 0,
        verification_status: "unavailable", verification_reason: "verifier_unavailable" },
    ];
    render(<ClaimVerdicts claims={measured} passages={passages} />);
    expect(screen.getByText("Possible contradiction")).toBeInTheDocument();
    expect(screen.getByText("Unresolved")).toBeInTheDocument();
    expect(screen.getByText("Not checked")).toBeInTheDocument();
    expect(screen.getByText(/The evidence check could not run/)).toBeInTheDocument();
    expect(screen.queryByText(/Support estimate 0%/)).not.toBeInTheDocument();
  });

  it("renders the claim verification summary", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    // summary text shown in the collapsed state (details not open)
    expect(screen.getByText("Claim verification")).toBeInTheDocument();
    expect(screen.getByText("2 claims need attention")).toBeInTheDocument();
  });

  it("renders all claims in the details list", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    expect(screen.getByText("Solid-state batteries are in mass production.")).toBeInTheDocument();
    expect(screen.getByText("Costs are competitive with Li-ion.")).toBeInTheDocument();
    expect(screen.getByText("Pilot plants exist.")).toBeInTheDocument();
  });

  it("shows verdict chips", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    expect(screen.getByText("Unsupported")).toBeInTheDocument();
    expect(screen.getByText("Weak")).toBeInTheDocument();
    expect(screen.getByText("Supported")).toBeInTheDocument();
  });

  it("shows entailment scores as percentages", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    expect(screen.getByText(/Score 12%/)).toBeInTheDocument();
    expect(screen.getByText(/Score 45%/)).toBeInTheDocument();
    expect(screen.getByText(/Score 91%/)).toBeInTheDocument();
  });

  it("shows links to best passage source", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    const links = screen.getAllByRole("link");
    // Each claim has a link to its best passage
    expect(links.length).toBeGreaterThanOrEqual(3);
    expect(links.some((l) => l.getAttribute("href") === "https://example.com/paper")).toBe(true);
  });

  it("returns null when claims array is empty", () => {
    const { container } = render(<ClaimVerdicts claims={[]} passages={passages} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows all-supported summary when no weak/unsupported claims", () => {
    const allSupported: VerifiedClaim[] = [
      {
        claim: { text: "Claim A.", cited_passage_ids: ["p0"] },
        verdict: "supported",
        best_passage_id: "p0",
        entailment_score: 0.95,
      },
      {
        claim: { text: "Claim B.", cited_passage_ids: ["p1"] },
        verdict: "supported",
        best_passage_id: "p1",
        entailment_score: 0.88,
      },
    ];
    render(<ClaimVerdicts claims={allSupported} passages={passages} />);
    expect(screen.getByText("All 2 claims supported")).toBeInTheDocument();
  });

  it("orders unsupported/weak before supported claims", () => {
    render(<ClaimVerdicts claims={claims} passages={passages} />);
    const items = screen.getAllByRole("listitem");
    // First item should be unsupported, then weak, then supported
    expect(items[0]).toHaveTextContent("Unsupported");
    expect(items[1]).toHaveTextContent("Weak");
    expect(items[2]).toHaveTextContent("Supported");
  });
});
