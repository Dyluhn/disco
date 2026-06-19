/**
 * A2 — latestEditableDeckBase gating. The Edit-Slides tab must appear ONLY when the
 * LATEST render of a base is editable. A later non-editable (Marp) regen of the same
 * base supersedes its earlier sidecar (mirrors the server's _sidecar_is_current guard)
 * — else the user edits stale source and overwrites the newer deck.
 */
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "@/types/agent";
import { latestEditableDeckBase } from "./AgentCanvas";

function obs(structured: Record<string, unknown>): AgentEvent {
  return {
    kind: "observation",
    tool_result: { tool_name: "slides_generate", success: true, structured },
  } as unknown as AgentEvent;
}

const editable = (base: string) =>
  obs({ filename: `${base}.html`, base_name: base, editable_source: `${base}.authored.json` });
const marp = (base: string) => obs({ filename: `${base}.html`, base_name: base });

describe("latestEditableDeckBase", () => {
  it("returns the base when the latest render is editable", () => {
    expect(latestEditableDeckBase([editable("deck")])).toBe("deck");
  });

  it("returns null when the latest render of the base is a non-editable Marp regen", () => {
    // editable C2 first, THEN a non-editable Marp render of the same base.
    expect(latestEditableDeckBase([editable("deck"), marp("deck")])).toBeNull();
  });

  it("ignores a non-editable deck but still offers a different editable base", () => {
    expect(latestEditableDeckBase([marp("notes"), editable("pitch")])).toBe("pitch");
  });

  it("returns null when there is no slides deck at all", () => {
    expect(latestEditableDeckBase([])).toBeNull();
  });

  it("does not offer a nested (slash) base — the route jails it, the tab would 404", () => {
    expect(latestEditableDeckBase([editable("reports/q2")])).toBeNull();
  });
});
