/**
 * Regression (NON-FROZEN, R4) — `downloadProject`'s source-binding behaviour at the
 * REAL URL-construction + fetch boundary. Complements the frozen G08 contract with
 * (a) a SECOND, distinct live binding proving no value is hardcoded, (b) the live
 * null-binding no-query guard, and (c) the OFFLINE observable-request mechanism the
 * frozen e2e depends on (a bound offline download issues a real anchor navigation to
 * the bound URL; an unbound offline download stays a no-op).
 *
 * Seams are the same category as the frozen G08 test: the runtime-config global
 * `__DISCO_ENV` (the `/env.js` seam) toggles live vs offline, a real recording
 * `fetch` captures the live URL, and jsdom shims (`URL.createObjectURL` +
 * `HTMLAnchorElement.prototype.click`) service APIs jsdom omits. Modules are loaded
 * FRESH per case (`vi.resetModules()`) so `client.ts` reads the configured base at
 * module load.
 */

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type RtEnv = { API_BASE?: string; AGENT_BASE?: string };
const g = globalThis as typeof globalThis & { __DISCO_ENV?: RtEnv };

const AGENT_BASE = "http://agent.test";

const savedEnv = g.__DISCO_ENV;
const savedFetch = globalThis.fetch;
const urlStatic = URL as unknown as {
  createObjectURL?: (o: Blob | MediaSource) => string;
  revokeObjectURL?: (u: string) => void;
};
const savedCreate = urlStatic.createObjectURL;
const savedRevoke = urlStatic.revokeObjectURL;
const savedClick = HTMLAnchorElement.prototype.click;

/** Load a FRESH `projects` module against the given runtime env (undefined → offline,
 * so `agentLive()` is false). `client.ts` snapshots the base at module load, so the
 * env must be set BEFORE the (re)import. */
async function loadProjects(env: RtEnv | undefined) {
  if (env) g.__DISCO_ENV = env;
  else delete g.__DISCO_ENV;
  vi.resetModules();
  return import("./projects");
}

beforeEach(() => {
  urlStatic.createObjectURL = () => "blob:reg-test";
  urlStatic.revokeObjectURL = () => {};
});

afterEach(() => {
  globalThis.fetch = savedFetch;
  HTMLAnchorElement.prototype.click = savedClick;
});

afterAll(() => {
  g.__DISCO_ENV = savedEnv;
  urlStatic.createObjectURL = savedCreate;
  urlStatic.revokeObjectURL = savedRevoke;
});

describe("downloadProject — the LIVE download URL binds to the supplied (version_seq, spec_digest)", () => {
  let recorded: string | undefined;
  beforeEach(() => {
    recorded = undefined;
    HTMLAnchorElement.prototype.click = () => {};
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const u = String(input);
      if (u.endsWith("/api/auth/session")) {
        return new Response(JSON.stringify({ authenticated: true, csrf_token: "reg-csrf" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      recorded = u;
      return new Response(new Blob([new Uint8Array([80, 75, 3, 4])]), {
        status: 200,
        headers: { "content-type": "application/zip" },
      });
    }) as typeof fetch;
  });

  it("a bound download carries the EXACT, correctly-encoded binding on the query", async () => {
    const { downloadProject } = await loadProjects({ AGENT_BASE });
    await downloadProject("conv_reg_a", { version_seq: 11, spec_digest: "sha256:reg-a-11" });
    expect(recorded).toBeDefined();
    const sp = new URL(recorded as string).searchParams;
    expect(sp.get("version_seq")).toBe("11");
    expect(sp.get("spec_digest")).toBe("sha256:reg-a-11");
    // Real URL-encoding (`:` → `%3A`), not a raw splice.
    expect(recorded as string).toContain("spec_digest=sha256%3Areg-a-11");
  });

  it("a DIFFERENT binding produces a DISTINCT query (no hardcoded value can satisfy both)", async () => {
    const { downloadProject } = await loadProjects({ AGENT_BASE });
    await downloadProject("conv_reg_b", { version_seq: 99, spec_digest: "sha256:reg-b-99" });
    expect(recorded).toBeDefined();
    const sp = new URL(recorded as string).searchParams;
    expect(sp.get("version_seq")).toBe("99");
    expect(sp.get("spec_digest")).toBe("sha256:reg-b-99");
    expect(recorded as string).toContain("spec_digest=sha256%3Areg-b-99");
  });

  it("a null binding fabricates NO query", async () => {
    const { downloadProject } = await loadProjects({ AGENT_BASE });
    await downloadProject("conv_reg_c", null);
    expect(recorded).toBeDefined();
    const url = new URL(recorded as string);
    expect(url.search).toBe("");
    expect(url.searchParams.has("version_seq")).toBe(false);
    expect(url.searchParams.has("spec_digest")).toBe(false);
  });
});

describe("downloadProject — OFFLINE: a bound download is browser-observable; an unbound one is a no-op", () => {
  let clickedHref: string | null;
  beforeEach(() => {
    clickedHref = null;
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
      clickedHref = this.href;
    };
    // The offline path must never touch the network — a fetch here is a defect.
    globalThis.fetch = (async () => {
      throw new Error("offline downloadProject must not fetch");
    }) as typeof fetch;
  });

  it("a bound offline download issues a real anchor navigation to the bound URL", async () => {
    const { downloadProject } = await loadProjects(undefined);
    await downloadProject("conv_demo_snake", {
      version_seq: 3,
      spec_digest: "sha256:demo-candidate-0001",
    });
    expect(clickedHref).toBeTruthy();
    const sp = new URL(clickedHref as string).searchParams;
    expect(sp.get("version_seq")).toBe("3");
    expect(sp.get("spec_digest")).toBe("sha256:demo-candidate-0001");
    expect(clickedHref as string).toContain("/api/projects/conv_demo_snake/download?");
    expect(clickedHref as string).toContain("spec_digest=sha256%3Ademo-candidate-0001");
  });

  it("an unbound offline download makes no request and no anchor navigation", async () => {
    const { downloadProject } = await loadProjects(undefined);
    await downloadProject("conv_demo_snake", null);
    expect(clickedHref).toBeNull();
  });
});

describe("isSnapshottedDemoProject — the reachability registry (drives offline resume rehydration)", () => {
  it("is true for snapshotted demo projects and false for the fresh-run + unknown cids", async () => {
    const { isSnapshottedDemoProject } = await loadProjects(undefined);
    expect(isSnapshottedDemoProject("conv_demo_snake")).toBe(true);
    expect(isSnapshottedDemoProject("conv_demo_landing")).toBe(true);
    expect(isSnapshottedDemoProject("conv_demo_orphan")).toBe(true);
    // The fresh-run fixture cid has no persisted snapshot → never auto-finishes.
    expect(isSnapshottedDemoProject("conv_fixture")).toBe(false);
    expect(isSnapshottedDemoProject("conv_unknown")).toBe(false);
  });
});
