import { describe, expect, it } from "vitest";
import { readableRequestError } from "./errors";

describe("readable form errors", () => {
  it("explains invalid bundles without dumping response metadata", () => {
    const error = new Error(JSON.stringify({ detail: { reason: "malformed_event", input: "private contents" } }));
    expect(readableRequestError(error, "Import failed")).toBe("This run bundle contains an invalid event. No conversation was imported.");
  });
  it("preserves useful policy explanations and validation messages", () => {
    expect(readableRequestError(new Error('{"detail":{"message":"Destination must be approved first"}}'), "Failed")).toBe("Destination must be approved first");
    expect(readableRequestError(new Error('{"detail":[{"msg":"URL must use https","input":"secret"}]}'), "Failed")).toBe("URL must use https");
  });
  it("handles malformed files, unknown structured errors and network failures", () => {
    expect(readableRequestError(new SyntaxError("unexpected token"), "Failed")).toMatch(/not valid JSON/);
    expect(readableRequestError(new Error('{"detail":{"code":777,"input":"secret"}}'), "Failed")).toBe("Failed");
    expect(readableRequestError(new Error("Network disconnected"), "Failed")).toBe("Network disconnected");
  });
});
