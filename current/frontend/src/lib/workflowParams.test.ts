import { describe, expect, it } from "vitest";
import { initialWorkflowParams, parseWorkflowParams } from "./workflowParams";

describe("workflow parameter parsing", () => {
  const schema = {
    properties: {
      query: { type: "string" },
      limit: { type: "integer" },
      tags: { type: "array" },
      options: { type: "object" },
      empty: { type: "null" },
    },
    required: ["query"],
  };

  it("parses the same schema shape used by Run and Scheduling", () => {
    expect(
      parseWorkflowParams(schema, {
        query: "weekly digest",
        limit: "3",
        tags: '["ai", "safety"]',
        options: '{"audience":"engineers"}',
        empty: "null",
      }),
    ).toEqual({
      params: {
        query: "weekly digest",
        limit: 3,
        tags: ["ai", "safety"],
        options: { audience: "engineers" },
        empty: null,
      },
      errors: {},
    });
  });

  it("rejects missing required values without creating a partial payload", () => {
    expect(parseWorkflowParams(schema, { query: "", limit: "bad", tags: "nope" })).toEqual({
      params: {},
      errors: {
        query: "Required",
        limit: "Enter a whole number",
        tags: 'Enter a JSON array, for example ["value"]',
      },
    });
  });

  it("keeps persisted empty params distinct from simulation fixtures", () => {
    expect(initialWorkflowParams(schema, {})).toEqual({
      query: "",
      limit: "",
      tags: "",
      options: "",
      empty: "",
    });
    expect(initialWorkflowParams(schema, { query: "fixture" })).toEqual({
      query: "fixture",
      limit: "",
      tags: "",
      options: "",
      empty: "",
    });
  });
});
