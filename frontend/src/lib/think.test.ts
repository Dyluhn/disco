/**
 * W-02 — splitThink: separate inline `<think>…</think>` reasoning from the
 * answer so the feed can collapse reasoning instead of leaking raw tags.
 */

import { describe, expect, it } from "vitest";
import { splitThink } from "@/lib/think";

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
});
