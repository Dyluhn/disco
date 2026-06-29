/**
 * productEvidence — P1B-LIVE-1 (the TS evidence-assembly bridge).
 *
 * The live browser product harness (P1B-LIVE-2/3) captures raw observations from the real
 * UI/WS/preview run; this turns them into the EXACT `product_evidence` dict shape the
 * Python writer (harness/build_soak/product_evidence.py `_SLICE_FIELDS`) validates and the
 * HARN-2 browser oracles read. A slice is included ONLY when its observation was captured —
 * an un-captured slice is omitted so the matching oracle SKIPs (the optional-slice contract);
 * there are NO fake-passing defaults.
 *
 * `SLICE_FIELDS` is the runtime mirror of the Python `_SLICE_FIELDS`; a Python parity test
 * (test_product_evidence_parity.py) asserts the two stay identical so they can never drift.
 */

export interface BrowserWsObs {
  connections: number;
}
export interface LifecycleObs {
  terminal: string;
  statuses: string[];
}
export interface SidecarObs {
  stopped_at_terminal: boolean;
  provider_calls_after_terminal: number;
}
export interface PreviewObs {
  owner: string;
  manual_port: boolean;
}
export interface ShownObs {
  artifact_shown: boolean;
  preview_shown: boolean;
}
export interface VerificationObs {
  ready_for_verification_called: boolean;
  passed: boolean;
}
export interface ExportObs {
  requested: boolean;
  download_present: boolean;
  download_bytes: number;
}
export interface CleanupObs {
  orphans: number;
  workspace_released: boolean;
}

/** Captured observations — a slice is present iff that part of the run was observed. */
export interface ProductObservations {
  browser_ws?: BrowserWsObs;
  lifecycle?: LifecycleObs;
  sidecar?: SidecarObs;
  preview?: PreviewObs;
  shown?: ShownObs;
  verification?: VerificationObs;
  export?: ExportObs;
  cleanup?: CleanupObs;
}

export type ProductEvidence = ProductObservations;

/** The slice keys — derived from the typed observation shape so they cannot drift. */
export type SliceKey = keyof ProductObservations;

/**
 * Runtime mirror of Python `_SLICE_FIELDS` (keys + field names) AND the single source the
 * assembler iterates. Annotated `Record<SliceKey, …>` so TS forces it to carry EXACTLY the
 * observation slices (a dropped/renamed slice is a compile error); a Python parity test
 * asserts it equals `_SLICE_FIELDS`.
 */
export const SLICE_FIELDS: Record<SliceKey, readonly string[]> = {
  browser_ws: ["connections"],
  lifecycle: ["terminal", "statuses"],
  sidecar: ["stopped_at_terminal", "provider_calls_after_terminal"],
  preview: ["owner", "manual_port"],
  shown: ["artifact_shown", "preview_shown"],
  verification: ["ready_for_verification_called", "passed"],
  export: ["requested", "download_present", "download_bytes"],
  cleanup: ["orphans", "workspace_released"],
};

/** All slice keys, DERIVED from SLICE_FIELDS — never a hand-maintained parallel list. */
export const SLICE_KEYS = Object.keys(SLICE_FIELDS) as SliceKey[];

/**
 * Assemble the `product_evidence` dict from captured observations. Iterates SLICE_FIELDS (the
 * single source) and includes a slice ONLY when it was actually observed (no fabricated
 * defaults) — an omitted slice SKIPs its oracle.
 */
export function buildProductEvidence(obs: ProductObservations): ProductEvidence {
  const ev: Record<string, unknown> = {};
  for (const key of Object.keys(SLICE_FIELDS) as SliceKey[]) {
    const slice = obs[key];
    if (slice !== undefined) ev[key] = slice;
  }
  return ev as ProductEvidence;
}
