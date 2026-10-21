import { describe, expect, it } from "vitest";
import type { WorkflowReview } from "@/types/workflow";
import { authorInputFromReview } from "./draftMapping";

function review(definition: unknown): WorkflowReview {
  return { name: "Local note", card: "Write a fixed note", definition } as WorkflowReview;
}

describe("review parameter fidelity", () => {
  it("does not invent a query for parameterless approved definitions", () => {
    const mapped = authorInputFromReview(review({
      params_model_schema: { type: "object", properties: {}, required: [] },
      params: [{ name: "stale_query", type: "string" }],
      output_contract: { path_template: "reports/note.md", format: "markdown" },
    }));
    expect(mapped.params).toEqual([]);
    expect(mapped.output_path_template).toBe("reports/note.md");
  });
  it("retains declared optional and required fields", () => {
    const mapped = authorInputFromReview(review({ params_model_schema: {
      type: "object", properties: { topic: { type: "string" }, count: { type: "integer" } }, required: ["topic"],
    } }));
    expect(mapped.params.map(({ name, type, required }) => ({ name, type, required }))).toEqual([
      { name: "topic", type: "string", required: true },
      { name: "count", type: "integer", required: false },
    ]);
  });
  it("preserves legacy friendly parameters when no schema exists", () => {
    expect(authorInputFromReview(review({ params: [{ name: "topic", type: "string" }] })).params[0].name).toBe("topic");
    expect(authorInputFromReview(review({ params: [] })).params).toEqual([]);
  });
});
