/**
 * W-02 — splitThink: separate inline `<think>…</think>` reasoning from the
 * answer so the feed can collapse reasoning instead of leaking raw tags.
 */

import { describe, expect, it } from "vitest";
import { splitThink, stripReasoningPrefixes } from "@/lib/think";

describe("splitThink", () => {
  it("splits a single closed block into reasoning + answer", () => {
    const { reasoning, answer } = splitThink("<think>weighing options</think>Here is the plan.");
    expect(reasoning).toBe("weighing options");
    expect(answer).toBe("Here is the plan.");
  });

  it("treats an unclosed trailing <think> as reasoning-in-progress (streaming)", () => {
    const { reasoning, answer } = splitThink("Answer so far<think>still thinking abou");
    expect(answer).toBe("Answer so far");
    expect(reasoning).toBe("still thinking abou");
  });

  it("passes plain text through untouched (no think tags)", () => {
    const { reasoning, answer } = splitThink("Just a normal answer, no tags.");
    expect(reasoning).toBe("");
    expect(answer).toBe("Just a normal answer, no tags.");
  });

  it("joins multiple blocks in order and keeps surrounding answer text", () => {
    const { reasoning, answer } = splitThink(
      "<think>first</think>visible one <think>second</think>visible two",
    );
    expect(reasoning).toBe("first\n\nsecond");
    expect(answer).toBe("visible one visible two");
  });

  it("is case-insensitive on the tags", () => {
    const { reasoning, answer } = splitThink("<THINK>caps</THINK>done");
    expect(reasoning).toBe("caps");
    expect(answer).toBe("done");
  });

  it("returns empty fields for empty / nullish input", () => {
    expect(splitThink("")).toEqual({ reasoning: "", answer: "" });
    expect(splitThink(null)).toEqual({ reasoning: "", answer: "" });
    expect(splitThink(undefined)).toEqual({ reasoning: "", answer: "" });
  });

  it("strips leaked stacked 'Reasoning:' decoration before splitting (MiniMax)", () => {
    // The live defect: MiniMax echoed the View's surface-form prefix, stacking it.
    const { reasoning, answer } = splitThink(
      "Reasoning: Reasoning: Reasoning: The planner returned an empty plan.",
    );
    expect(answer).toBe("The planner returned an empty plan.");
    expect(reasoning).toBe("");
  });
});

describe("stripReasoningPrefixes — leaked surface-form decoration", () => {
  it("collapses a stack of 'Reasoning:' prefixes", () => {
    expect(stripReasoningPrefixes("Reasoning: Reasoning: Reasoning: done")).toBe("done");
  });

  it("strips a mixed 'Thought:' + 'Reasoning:' stack", () => {
    expect(stripReasoningPrefixes("Thought: Reasoning: Reasoning: go")).toBe("go");
  });

  it("leaves a clean thought untouched", () => {
    expect(stripReasoningPrefixes("Build verified — HTTP 200.")).toBe("Build verified — HTTP 200.");
  });

  it("only strips leading decorators, not mid-sentence ones", () => {
    expect(stripReasoningPrefixes("Stale tracker. Reasoning: it missed a delta.")).toBe(
      "Stale tracker. Reasoning: it missed a delta.",
    );
  });

  it("is idempotent and null-safe", () => {
    expect(stripReasoningPrefixes(stripReasoningPrefixes("Reasoning: x"))).toBe("x");
    expect(stripReasoningPrefixes(null)).toBe("");
    expect(stripReasoningPrefixes(undefined)).toBe("");
  });
});
