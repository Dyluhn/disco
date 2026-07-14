/**
 * WO-A (G08 / G11) — the workspace-zip download URL MUST carry the self-host
 * binding (`version_seq` + `spec_digest`) that the operator was shown, proven at the
 * REAL URL-construction + fetch boundary and INDEPENDENT of any panel/component
 * render.
 *
 * SELF-DISCRIMINATING, NON-REWRITEABLE contract (the R4 production shape is pinned
 * HERE, not deferred). The download boundary's R4 signature is
 *
 *     downloadProject(conversationId, binding)
 *          binding = { version_seq: number; spec_digest: string } | null
 *
 * and THIS TEST SUPPLIES the binding to that boundary. Because the test passes the
 * values in, production can legitimately place the SUPPLIED values on the URL without
 * knowing them a priori from a bare cid — which is exactly why this frozen test flips
 * green at R4 through PRODUCTION ALONE, with NO edit to this file:
 *
 *   - TODAY  `api/projects.ts` exports the 1-arg `downloadProject(cid)`. It ignores
 *            the 2nd runtime argument entirely and builds a cid-only URL
 *            (`/api/projects/{cid}/download`, no query), so the supplied binding never
 *            reaches the URL -> RED at the binding-value assertion.
 *   - AT R4  production adopts the 2-arg signature and appends the SUPPLIED binding as
 *            `?version_seq=…&spec_digest=…` (URL-encoded). The URL then carries EXACTLY
 *            the values the test passed in -> GREEN. No test change is needed: the
 *            expected values ARE the test's own inputs, never a local fixture that
 *            production could not derive from a bare cid.
 *
 * This is deliberately NOT the old "cid-only, fixture-expected" red (whose expected
 * values came from a local fixture that production cannot derive from a bare cid, so it
 * could not turn green through a production fix alone): here every expectation is
 * DERIVED FROM THE SUPPLIED INPUT, and two MATERIALLY different bindings are exercised
 * through the SAME assertions, so no hardcoded query value can satisfy both. The only
 * way to green is a genuine supplied-binding -> URL wiring in production; this frozen
 * test never needs editing.
 *
 * Boundary reached WITHOUT rendering any component: the test seams the system only at
 * (1) the runtime-config loader `globalThis.__DISCO_ENV` — the very `/env.js` seam the
 * self-host nginx writes, so `AGENT_BASE` is configured and `downloadProject` builds a
 * live URL instead of returning early — and (2) the network boundary, via a REAL
 * recording `globalThis.fetch` (a genuine async function returning a real `Response`,
 * NOT a runner mock) that captures the exact requested URL. `projects.ts` is imported
 * DYNAMICALLY, after the config seam is set, so `AGENT_BASE` reads the configured base
 * at module load.
 *
 * Anti-bypass (plan §4.4): no runner module-replacement / spy / global-stub / env-stub
 * helper is used; the runner's mock API is not imported at all. The seams are plain
 * saved/restored assignments to `globalThis.__DISCO_ENV` (config loader) and
 * `globalThis.fetch` (network boundary), plus jsdom ENVIRONMENT shims —
 * `URL.createObjectURL`/`revokeObjectURL` and a no-op `HTMLAnchorElement.prototype.click`
 * — that jsdom omits or cannot service. Those shims are the same category as the
 * object-URL shim already required here, NOT a product mock: the URL is captured at
 * fetch time, BEFORE the anchor click, so neutralizing the click cannot affect any
 * assertion; it only silences jsdom's "Not implemented: navigation" stderr noise.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

/** The runtime-config shape the self-host `/env.js` writes onto the global. */
type RtEnv = { API_BASE?: string; AGENT_BASE?: string };

/**
 * The R4 production contract this frozen test pins. A BOUND download names the
 * assessed release it must pin to (both fields concrete); `null` is an UNBOUND
 * (needs-review) release that must NOT fabricate any binding query. Casting the
 * exported `downloadProject` to this 2-arg shape lets the test SUPPLY the binding to
 * the real boundary while still compiling against today's 1-arg signature — production
 * is never touched to make this compile or to make it flip green.
 */
type ReleaseBinding = { version_seq: number; spec_digest: string };
type BoundDownload = (cid: string, binding: ReleaseBinding | null) => Promise<void>;

const AGENT_BASE = "http://agent.test";

// (1) Runtime-config seam — MUST be set at module top level, BEFORE the dynamic import
// inside any test, because client.ts reads AGENT_BASE from this global at module load.
// This is the identical seam the self-host image populates.
const globalEnv = globalThis as typeof globalThis & { __DISCO_ENV?: RtEnv };
const savedDiscoEnv = globalEnv.__DISCO_ENV;
globalEnv.__DISCO_ENV = { AGENT_BASE };

// (2) Network boundary — a real recorder installed per test; original saved here.
const savedFetch = globalThis.fetch;
let recordedUrl: string | undefined;

// jsdom lacks the object-URL APIs the download path calls after res.blob().
const urlStatic = URL as unknown as {
  createObjectURL: ((obj: Blob | MediaSource) => string) | undefined;
  revokeObjectURL: ((objectUrl: string) => void) | undefined;
};
const savedCreateObjectURL = urlStatic.createObjectURL;
const savedRevokeObjectURL = urlStatic.revokeObjectURL;

// jsdom cannot service the download path's `<a>.click()` navigation to the blob URL
// and logs "Not implemented: navigation (except hash changes)" to stderr. The URL is
// already captured at fetch time (well before the click), so a no-op click is a pure
// environment shim — it changes nothing the assertions observe, it only silences noise.
const savedAnchorClick = HTMLAnchorElement.prototype.click;

/** Load the REAL download boundary and view it through the R4 2-arg contract. The
 * dynamic import runs AFTER the config seam is set (see top of file), so `AGENT_BASE`
 * is read at module load and `downloadProject` builds a live URL. */
async function loadBoundDownload(): Promise<BoundDownload> {
  const mod = await import("../../api/projects");
  return mod.downloadProject as unknown as BoundDownload;
}

beforeAll(() => {
  urlStatic.createObjectURL = () => "blob:g08-test";
  urlStatic.revokeObjectURL = () => {};
  HTMLAnchorElement.prototype.click = () => {};
});

afterAll(() => {
  urlStatic.createObjectURL = savedCreateObjectURL;
  urlStatic.revokeObjectURL = savedRevokeObjectURL;
  HTMLAnchorElement.prototype.click = savedAnchorClick;
  globalEnv.__DISCO_ENV = savedDiscoEnv;
});

beforeEach(() => {
  recordedUrl = undefined;
  // A REAL async network boundary (not a runner mock): it answers the auth session
  // probe so session setup succeeds, and records the download URL verbatim.
  const recorder: typeof fetch = async (input) => {
    const requested = String(input);
    if (requested.endsWith("/api/auth/session")) {
      return new Response(JSON.stringify({ authenticated: true, csrf_token: "g08-test-csrf" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    // The workspace-zip download boundary — capture the exact requested URL STRING, so
    // the URL-ENCODING of the query is observable (not just the parsed-back values).
    recordedUrl = requested;
    return new Response(new Blob([new Uint8Array([80, 75, 3, 4])]), {
      status: 200,
      headers: { "content-type": "application/zip" },
    });
  };
  globalThis.fetch = recorder;
});

afterEach(() => {
  globalThis.fetch = savedFetch;
});

// Two MATERIALLY different bound releases the test knows the truth of — distinct
// version_seq AND distinct spec_digest — run through the SAME assertions, so no single
// hardcoded query value can satisfy both. `encoded` is the exact URL-ENCODED substring
// the raw request URL must contain (`:` -> `%3A`), proving production ENCODES the value
// rather than splicing it in raw.
const boundCases: ReadonlyArray<{
  label: string;
  cid: string;
  binding: ReleaseBinding;
  encoded: string;
}> = [
  {
    label: "v7 / g08a-0007",
    cid: "conv_g08a",
    binding: { version_seq: 7, spec_digest: "sha256:g08a-0007" },
    encoded: "spec_digest=sha256%3Ag08a-0007",
  },
  {
    label: "v42 / g08b-0042",
    cid: "conv_g08b",
    binding: { version_seq: 42, spec_digest: "sha256:g08b-0042" },
    encoded: "spec_digest=sha256%3Ag08b-0042",
  },
];

describe("WO-A (G08) — the download URL binds to the release's version_seq + spec_digest", () => {
  it.each(boundCases)(
    "carries the SUPPLIED binding [$label] on the download URL",
    async ({ cid, binding, encoded }) => {
      // Reach the REAL URL-construction boundary (no component render at all) and
      // SUPPLY the binding through the R4 2-arg contract.
      const boundDownload = await loadBoundDownload();
      await boundDownload(cid, binding);

      // The download must have reached the network boundary FIRST — so any failure
      // below is the omitted binding, NOT an unreached or misconfigured seam.
      expect(recordedUrl, "downloadProject must reach the network fetch boundary").toBeDefined();
      const raw = recordedUrl as string;
      const sp = new URL(raw).searchParams;

      // THE TEETH — the SUPPLIED C2 binding must ride the download URL with its EXACT
      // values (not mere presence). RED today: projects.ts builds
      // `/api/projects/{cid}/download` with no query, ignoring the supplied binding, so
      // both params are absent and `.get(...)` is null.
      expect(sp.get("version_seq")).toBe(String(binding.version_seq));
      expect(sp.get("spec_digest")).toBe(binding.spec_digest);

      // …and the RAW request URL must carry the CORRECTLY URL-ENCODED param, so green
      // demands real encoding (URLSearchParams / encodeURIComponent), not a raw splice.
      expect(raw).toContain(encoded);
    },
  );

  it("G11: an unbound release (binding=null) must not fabricate a binding on the URL", async () => {
    const boundDownload = await loadBoundDownload();
    // A needs-review release with no pinned spec: supply an explicit null binding.
    await boundDownload("conv_g08unbound", null);

    expect(recordedUrl, "downloadProject must reach the network fetch boundary").toBeDefined();
    const sp = new URL(recordedUrl as string).searchParams;

    // No real binding exists, so none may be invented onto the URL. Holds TODAY
    // (cid-only URL) and must keep holding at R4 (a null binding appends nothing).
    expect(sp.has("version_seq"), "a null binding must not fabricate version_seq").toBe(false);
    expect(sp.get("spec_digest")).toBeNull();
  });
});
