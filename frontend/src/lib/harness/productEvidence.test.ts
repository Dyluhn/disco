import { describe, expect, it } from "vitest";

import {
  type ProductObservations,
  SLICE_FIELDS,
  SLICE_KEYS,
  buildProductEvidence,
} from "@/lib/harness/productEvidence";

function greenObservations(): ProductObservations {
  return {
    browser_ws: { connections: 1 },
    lifecycle: { terminal: "FINISHED", statuses: ["RUNNING", "FINISHED"] },
    sidecar: { stopped_at_terminal: true, provider_calls_after_terminal: 0 },
    preview: { owner: "platform", manual_port: false },
    shown: { artifact_shown: true, preview_shown: true },
    verification: { ready_for_verification_called: true, passed: true },
    export: { requested: true, download_present: true, download_bytes: 4096 },
    cleanup: {
      orphans: 0,
      workspace_released: true,
      scope: "conversation",
      container_orphans: 0,
      volume_orphans: 0,
      volume_scope: "conversation",
    },
  };
}

describe("buildProductEvidence", () => {
  it("SLICE_KEYS is derived from SLICE_FIELDS (no parallel-list drift)", () => {
    expect([...SLICE_KEYS].sort()).toEqual(Object.keys(SLICE_FIELDS).sort());
  });

  it("assembles every captured slice with its exact fields", () => {
    const green = greenObservations();
    const ev = buildProductEvidence(green) as Record<string, Record<string, unknown>>;
    // assert against the INPUT keys (not SLICE_KEYS) so a dropped slice key can't hide a loss
    expect(Object.keys(ev).sort()).toEqual(Object.keys(green).sort());
    for (const key of SLICE_KEYS) {
      expect(Object.keys(ev[key]).sort()).toEqual([...SLICE_FIELDS[key]].sort());
    }
    // field types match the schema (connections number, flags bool, bytes number)
    expect(typeof ev.browser_ws.connections).toBe("number");
    expect(typeof ev.preview.manual_port).toBe("boolean");
    expect(typeof ev.export.download_bytes).toBe("number");
  });

  it("omits an un-captured slice (no fabricated default → that oracle SKIPs)", () => {
    const obs = greenObservations();
    delete obs.export; // export not observed this run
    const ev = buildProductEvidence(obs);
    expect("export" in ev).toBe(false);
    expect("browser_ws" in ev).toBe(true);
  });

  it("an empty observation set yields an empty dossier (all oracles SKIP)", () => {
    expect(buildProductEvidence({})).toEqual({});
  });
});
