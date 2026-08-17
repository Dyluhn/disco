import { describe, expect, it } from "vitest";
import { forbiddenTestDiagnostic } from "./diagnostics";

describe("strict frontend diagnostic gate", () => {
  it.each([
    "An update to PreviewPane inside a test was not wrapped in act(...).",
    "A component suspended inside an `act` scope, but the `act` call was not awaited.",
    "You called act(async () => ...) without await.",
    new Error("Not implemented: navigation (except hash changes)"),
  ])("rejects a hidden async or navigation diagnostic: %s", (diagnostic) => {
    expect(forbiddenTestDiagnostic([diagnostic])).not.toBeNull();
  });

  it("preserves deliberate failure-path logging as visible console evidence", () => {
    expect(
      forbiddenTestDiagnostic(["Chart.js init failed", new Error("Rendering failed")]),
    ).toBeNull();
  });
});
