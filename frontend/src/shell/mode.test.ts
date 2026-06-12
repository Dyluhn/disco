import { describe, expect, it } from "vitest";
import { MODES, SCOPE_OPTIONS, defaultScope, type Mode } from "./mode";

/** The agent mode is a live, first-class top-level mode whose scope set is safe to
 * index. Regression for the `defaultScope` crash: an empty SCOPE_OPTIONS array would
 * throw on `[0]`, so every mode must expose at least one non-dormant scope. */
describe("mode model — agent surface", () => {
  it("agent is a live (non-dormant) top-level mode", () => {
    const agent = MODES.find((m) => m.id === "agent");
    expect(agent).toBeDefined();
    expect(agent?.dormant).toBe(false);
  });

  it("every mode has a non-throwing default scope (no empty SCOPE_OPTIONS)", () => {
    for (const m of MODES) {
      expect(() => defaultScope(m.id)).not.toThrow();
      expect(SCOPE_OPTIONS[m.id].length).toBeGreaterThan(0);
    }
  });

  it("agent's default scope is the single Standard option", () => {
    expect(defaultScope("agent")).toBe("standard");
  });

  it("SCOPE_OPTIONS is total over Mode (no missing key)", () => {
    const modes: Mode[] = ["search", "build", "agent"];
    for (const m of modes) expect(SCOPE_OPTIONS[m]).toBeDefined();
  });
});
