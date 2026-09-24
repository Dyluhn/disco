import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";
import { CitedText } from "./blocks/CitedText";
import type { GroundedAnswer, VerifiedClaim, Verdict } from "@/types/grounded";

const first = "`example.com` and `example.org` are reserved for documentation purposes and may be used as illustrative examples in documents without prior coordination";
const second = "They are not available for registration or transfer";

function claim(text: string, verdict: Verdict, ids = ["shared", "other"]): VerifiedClaim {
  return { claim: { text, cited_passage_ids: ids }, verdict, best_passage_id: ids[0], entailment_score: verdict === "supported" ? 0.523 : 0.0017 };
}
function answer(claims: VerifiedClaim[]): GroundedAnswer {
  return {
    query: "site:iana.org example domains", blocks: [], claims,
    passages: ["shared", "other"].map((id) => ({ id, source_url: `https://iana.org/${id}`, source_title: id, text: "Actual extracted passage" })),
    all_hits: [], unsupported_count: claims.filter((c) => c.verdict === "unsupported").length,
    follow_ups: [],
  };
}
const mixed = answer([claim(first, "unsupported"), claim(second, "supported")]);
const prose = `${first} [[shared]][[other]]. ${second} [[shared]][[other]].`;

function labels() { return screen.getAllByRole("button").map((button) => button.getAttribute("aria-label")); }

// Real citation buttons, not a mock of verdictForPassage or the renderer. The
// shared IDs and opposite verdicts reproduce the captured Qwen final answer.
describe("citation verdict belongs to the claim occurrence", () => {
  it("uses each claim's verdict for repeated sources in one paragraph and source card", async () => {
    const user = userEvent.setup();
    render(<Markdown answer={mixed}>{prose}</Markdown>);
    expect(labels()).toEqual([
      "Source 1: shared (Not supported by the cited source)",
      "Source 2: other (Not supported by the cited source)",
      "Source 1: shared (Supported by this source)",
      "Source 2: other (Supported by this source)",
    ]);
    await user.click(screen.getByRole("button", { name: "Source 1: shared (Supported by this source)" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Supported by this source");
    expect(screen.getByRole("dialog")).toHaveTextContent("Actual extracted passage");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not mark a later unsupported occurrence green when the first is supported", () => {
    render(<Markdown answer={answer([claim(first, "supported"), claim(second, "unsupported")])}>{prose}</Markdown>);
    expect(labels().slice(0, 2).every((label) => label?.includes("(Supported by this source)"))).toBe(true);
    expect(labels().slice(2).every((label) => label?.includes("(Not supported by the cited source)"))).toBe(true);
  });

  it("claim array order does not change occurrence attribution", () => {
    render(<Markdown answer={{ ...mixed, claims: [...mixed.claims].reverse() }}>{prose}</Markdown>);
    expect(labels().slice(0, 2).every((label) => label?.includes("(Not supported by the cited source)"))).toBe(true);
    expect(labels().slice(2).every((label) => label?.includes("(Supported by this source)"))).toBe(true);
  });

  it("carries prose context across inline code, bold, italic and links in separate list items", () => {
    const a = answer([claim("- Domain example.com is **reserved**", "unsupported", ["shared"]), claim("- It cannot be *registered* with [IANA](https://iana.org)", "supported", ["shared"])]);
    render(<Markdown answer={a}>{"- Domain `example.com` is **reserved** [[shared]].\n- It cannot be *registered* with [IANA](https://iana.org) [[shared]]."}</Markdown>);
    expect(labels()).toEqual(["Source 1: shared (Not supported by the cited source)", "Source 1: shared (Supported by this source)"]);
  });

  it("keeps table cell occurrence context isolated", () => {
    const a = answer([claim("Allowed", "unsupported", ["shared"]), claim("Reserved", "supported", ["shared"])]);
    render(<Markdown answer={a}>{"| A | B |\n|---|---|\n| Allowed [[shared]] | Reserved [[shared]] |"}</Markdown>);
    expect(labels()).toEqual(["Source 1: shared (Not supported by the cited source)", "Source 1: shared (Supported by this source)"]);
  });

  it("applies the same claim binding to plain CitedText and adjacent citation groups", () => {
    render(<CitedText text={prose} answer={mixed} />);
    expect(labels().slice(0, 2).every((label) => label?.includes("(Not supported by the cited source)"))).toBe(true);
    expect(labels().slice(2).every((label) => label?.includes("(Supported by this source)"))).toBe(true);
  });

  it("retains the verdict of a complete sentence with an inline qualification", () => {
    const a = answer([claim("The domain is reserved, but unavailable for registration", "unsupported", ["shared"])]);
    render(<Markdown answer={a}>{"The domain is reserved [[shared]], but unavailable for registration [[shared]]."}</Markdown>);
    expect(labels()).toEqual(["Source 1: shared (Not supported by the cited source)", "Source 1: shared (Not supported by the cited source)"]);
  });

  it("does not borrow a supported verdict for unmatched prose or a partial-word suffix", () => {
    const a = answer([claim("safe", "supported", ["shared"])]);
    render(<Markdown answer={a}>{"Unknown prose [[shared]]. unsafe [[shared]]. not safe [[shared]]."}</Markdown>);
    expect(labels().every((label) => label?.includes("(Weakly supported — verify)"))).toBe(true);
  });

  it("does not guess which identical claim verdict applies when occurrence identity is absent", () => {
    const a = answer([claim(second, "supported", ["shared"]), claim(second, "unsupported", ["shared"])]);
    render(<Markdown answer={a}>{`${second} [[shared]].`}</Markdown>);
    expect(screen.getByRole("button")).toHaveAccessibleName("Source 1: shared (Weakly supported — verify)");
  });

  it("keeps literal code markers inert and unresolved stream markers neutral", () => {
    const { rerender } = render(<Markdown answer={null}>{`${second} [[shared]].`}</Markdown>);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("·")).toBeInTheDocument();
    rerender(<Markdown answer={mixed}>{"`literal [[shared]]`\n\n```text\n[[shared]]\n```\n\nMissing [[absent]]."}</Markdown>);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("literal [[shared]]")).toBeInTheDocument();
    expect(screen.getByText("·")).toBeInTheDocument();
  });
});
