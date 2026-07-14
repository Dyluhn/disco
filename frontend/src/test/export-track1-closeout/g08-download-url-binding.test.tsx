/**
 * WO-A (G08 / G11) — the workspace-zip download URL MUST carry the self-host
 * binding (`version_seq` + `spec_digest`), proven at the REAL URL-construction
 * boundary and INDEPENDENT of any panel/component render.
 *
 * Why this shape (plan §7 WO-A, grounded seam):
 *   `downloadProject(cid)` (frontend/src/api/projects.ts) builds
 *     `${agentHttpBase()}/api/projects/${encodeURIComponent(cid)}/download`
 *   — cid-only, with NO binding query — then hands it to `agentFetch` →
 *   `authFetch` → the bare global `fetch`, then blob → `URL.createObjectURL` →
 *   an `<a>` click. So the download that a self-host operator triggers is NOT
 *   pinned to the release version/spec it was assessed against (C2 binding).
 *
 * This test reaches that exact boundary WITHOUT rendering SelfHostPanel (or any
 * component): it seams the system only at (1) the runtime-config loader
 * `globalThis.__DISCO_ENV` — the very `/env.js` seam the self-host nginx writes,
 * so `AGENT_BASE` is configured and `downloadProject` builds a live URL instead
 * of returning early — and (2) the network boundary, by installing a REAL
 * recording `fetch` (a genuine async function returning a real `Response`, NOT a
 * runner mock). It then parses the requested URL and asserts the binding rides
 * it. RED today: the URL is cid-only, so both binding params are absent.
 *
 * Anti-bypass (plan §4.4): no runner module-replacement / spy / global-stub /
 * env-stub helper is used; the runner's mock API is not imported at all. The seams are plain
 * assignments to `globalThis.__DISCO_ENV` (config loader) and `globalThis.fetch`
 * (network boundary) — both saved and restored — plus a real jsdom shim for
 * `URL.createObjectURL`/`revokeObjectURL` (jsdom omits them). `projects.ts` is
 * imported DYNAMICALLY, after the config seam is set, so `AGENT_BASE` reads the
 * configured base at module load.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { ReleaseResponse } from "@/types/release";

/** The runtime-config shape the self-host `/env.js` writes onto the global. */
type RtEnv = { API_BASE?: string; AGENT_BASE?: string };

const AGENT_BASE = "http://agent.test";

// (1) Runtime-config seam — MUST be set at module top level, BEFORE the dynamic
// import inside any test, because client.ts reads AGENT_BASE from this global at
// module load. This is the identical seam the self-host image populates.
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

/**
 * A BOUND release the test knows the truth of: a real assessment carrying a
 * concrete `version_seq` + `spec_digest`. A download for THIS release must pin
 * to both. The expected values come from this fixture (the test's knowledge).
 */
function boundRelease(): ReleaseResponse {
  return {
    assessment: "candidate",
    reasons: ["A Node web server binding $PORT was detected (server.js)."],
    blockers: [],
    required_env: [{ name: "PORT", scope: "runtime", required: true, secret: false }],
    command: "docker compose up -d --build",
    ingress: { service: "web", port: "PORT", health_path: "/healthz" },
    self_host: true,
    spec_digest: "sha256:g08-bound-0007",
    version_seq: 7,
    tree_digest: "sha256:tree-g08-0007",
  };
}

/**
 * An UNBOUND release (G11): no spec has been pinned, so `spec_digest` is null.
 * The download must NOT invent a spec_digest onto the URL.
 */
function unboundRelease(): ReleaseResponse {
  return {
    assessment: "needs_review",
    reasons: ["Multiple web servers detected; owner must choose the entrypoint."],
    blockers: [{ code: "ambiguous_entrypoint", message: "pick one service", field: null, path: null }],
    required_env: [],
    command: "",
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 4,
    tree_digest: "sha256:tree-g08-unbound",
  };
}

beforeAll(() => {
  urlStatic.createObjectURL = () => "blob:g08-test";
  urlStatic.revokeObjectURL = () => {};
});

afterAll(() => {
  urlStatic.createObjectURL = savedCreateObjectURL;
  urlStatic.revokeObjectURL = savedRevokeObjectURL;
  globalEnv.__DISCO_ENV = savedDiscoEnv;
});

beforeEach(() => {
  recordedUrl = undefined;
  // A REAL async network boundary (not a runner mock): it answers the auth
  // session probe so session setup succeeds, and records the download URL.
  const recorder: typeof fetch = async (input) => {
    const requested = String(input);
    if (requested.endsWith("/api/auth/session")) {
      return new Response(JSON.stringify({ authenticated: true, csrf_token: "g08-test-csrf" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    // The workspace-zip download boundary — capture the exact requested URL.
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

describe("WO-A (G08) — the download URL binds to the release's version_seq + spec_digest", () => {
  it("carries the bound release's version_seq + spec_digest on the download URL", async () => {
    const release = boundRelease();

    // Reach the REAL URL-construction boundary (no component render at all).
    const { downloadProject } = await import("../../api/projects");
    await downloadProject("conv_g08demo");

    // The download must have reached the network boundary (proves the seam works
    // and this is not an env/reachability failure).
    expect(recordedUrl, "downloadProject must reach the network fetch boundary").toBeDefined();
    const sp = new URL(recordedUrl as string).searchParams;

    // THE TEETH — the C2 self-host binding must ride the download URL. RED today:
    // projects.ts builds `/api/projects/{cid}/download` with no query params, so
    // neither binding param is present.
    expect(sp.has("version_seq"), "download URL must carry the version_seq binding").toBe(true);
    expect(sp.has("spec_digest"), "download URL must carry the spec_digest binding").toBe(true);

    // And they must equal the assessed release's values (the test's knowledge).
    expect(sp.get("version_seq")).toBe(String(release.version_seq));
    expect(sp.get("spec_digest")).toBe(release.spec_digest);
  });

  it("G11: an unbound release (spec_digest=null) must not fabricate a binding on the URL", async () => {
    const release = unboundRelease();
    expect(release.spec_digest).toBeNull();

    const { downloadProject } = await import("../../api/projects");
    await downloadProject("conv_g08unbound");

    expect(recordedUrl, "downloadProject must reach the network fetch boundary").toBeDefined();
    const sp = new URL(recordedUrl as string).searchParams;

    // No real spec_digest exists, so none may be invented onto the URL. Holds now
    // (cid-only URL) and must keep holding once G08 wires binding for BOUND releases.
    expect(sp.get("spec_digest")).toBeNull();
  });
});
