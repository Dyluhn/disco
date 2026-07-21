/**
 * WO-8 — the frontend release-capabilities data layer (types + api + hook).
 *
 * Proves, offline (no live backend, the dev/demo path):
 *  1. `release.ts` mirrors the WO-7 `/release` shape EXACTLY — the assessment
 *     union is the SIX states, and `required_env` entries are `{name, scope,
 *     required, secret}` with NO `value` field (secret hygiene).
 *  2. `useProjectRelease` returns the fixture verdict for `candidate` /
 *     `needs_review` / `not_web` with no backend (the `fixtureProjects` pattern).
 *  3. Live, the api function hits `GET /api/projects/{cid}/release` on the AGENT
 *     base (`agentHttpBase`), never the app server.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getProjectRelease } from "@/api/projects";
import { useProjectRelease } from "@/hooks/useProjects";
import type { getProjectRelease as getProjectReleaseFn } from "@/api/projects";
import type { ReleaseAssessmentState, ReleaseEnv, ReleaseResponse } from "@/types/release";

type GetProjectRelease = typeof getProjectReleaseFn;

// ---- react-query wrapper (mirrors useSessions.test.ts) ---------------------

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

// ---- live-mode helpers (mirror projects.import.test.ts) --------------------

function jsonResponse(body: object, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

function makeFetchStub(body: object, status = 200) {
  return vi.fn(async (url: RequestInfo | URL) => {
    if (String(url) === "http://agent/api/auth/session") {
      return jsonResponse({ authenticated: true, csrf_token: "csrf-token" });
    }
    return jsonResponse(body, status);
  });
}

async function importLiveProjects(): Promise<{ getProjectRelease: GetProjectRelease }> {
  vi.resetModules();
  vi.stubGlobal("__DISCO_ENV", { AGENT_BASE: "http://agent" });
  return import("@/api/projects");
}

// ---- criterion 1: the type mirrors the WO-7 shape EXACTLY ------------------

// Compile-time proof the union is EXACTLY the six WO-7 assessment states: an
// extra literal here is a type error; a missing case makes `assertExhaustive`
// fail its `never` check below.
const ALL_STATES: ReleaseAssessmentState[] = [
  "not_web",
  "candidate",
  "needs_review",
  "verifying",
  "verified",
  "failed",
];

function assertExhaustive(state: ReleaseAssessmentState): ReleaseAssessmentState {
  switch (state) {
    case "not_web":
    case "candidate":
    case "needs_review":
    case "verifying":
    case "verified":
    case "failed":
      return state;
    default: {
      const unreachable: never = state;
      return unreachable;
    }
  }
}

function assertEnvShape(env: ReleaseEnv): void {
  // EXACTLY {name, scope, required, secret} — and crucially NO `value` field.
  expect(Object.keys(env).sort()).toEqual(["name", "required", "scope", "secret"]);
  expect("value" in env).toBe(false);
  expect(typeof env.name).toBe("string");
  expect(["runtime", "build"]).toContain(env.scope);
  expect(typeof env.required).toBe("boolean");
  expect(typeof env.secret).toBe("boolean");
}

// ---- tests -----------------------------------------------------------------

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("WO-8 release types (criterion 1)", () => {
  it("covers the six assessment states and only those", () => {
    expect(ALL_STATES).toHaveLength(6);
    for (const state of ALL_STATES) {
      expect(assertExhaustive(state)).toBe(state);
    }
  });

  it("fixture required_env entries are NAMES-only ({name,scope,required,secret}, no value)", async () => {
    // Offline (no backend stubbed) → the api returns the in-repo fixture verdicts.
    const candidate = await getProjectRelease("conv_demo_snake");
    expect(candidate.required_env.length).toBeGreaterThan(0);
    for (const env of candidate.required_env) assertEnvShape(env);
    // A secret env still carries only its NAME + `secret: true` flag, never a value.
    const secretEnv = candidate.required_env.find((e) => e.secret);
    expect(secretEnv).toBeDefined();
    expect("value" in (secretEnv as ReleaseEnv)).toBe(false);

    const needsReview = await getProjectRelease("conv_demo_landing");
    for (const env of needsReview.required_env) assertEnvShape(env);
  });
});

describe("useProjectRelease — offline fixture mode (criterion 2)", () => {
  const cases: Array<{ cid: string; assessment: ReleaseAssessmentState }> = [
    { cid: "conv_demo_snake", assessment: "candidate" },
    { cid: "conv_demo_landing", assessment: "needs_review" },
    { cid: "conv_demo_orphan", assessment: "not_web" },
  ];

  for (const { cid, assessment } of cases) {
    it(`returns the ${assessment} fixture for ${cid} with no live backend`, async () => {
      const { result } = renderHook(() => useProjectRelease(cid), { wrapper: makeWrapper() });
      await waitFor(() => expect(result.current.data).toBeDefined());
      const data = result.current.data as ReleaseResponse;
      expect(data.assessment).toBe(assessment);
      expect(data.command).toBe("docker compose up -d --build");
      // The stable key set is identical for every assessment.
      expect(Object.keys(data).sort()).toEqual(
        [
          "assessment",
          "blockers",
          "command",
          "ingress",
          "reasons",
          "required_env",
          "self_host",
          "spec_digest",
          "tree_digest",
          "version_seq",
        ].sort(),
      );
    });
  }

  it("candidate carries a resolved ingress + self_host; not_web carries neither", async () => {
    const { result: cand } = renderHook(() => useProjectRelease("conv_demo_snake"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(cand.current.data).toBeDefined());
    expect(cand.current.data?.self_host).toBe(true);
    expect(cand.current.data?.ingress?.port).toBe("PORT"); // env-var NAME, not a number

    const { result: notWeb } = renderHook(() => useProjectRelease("conv_demo_orphan"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(notWeb.current.data).toBeDefined());
    expect(notWeb.current.data?.self_host).toBe(false);
    expect(notWeb.current.data?.ingress).toBeNull();
  });
});

describe("getProjectRelease — live targets the AGENT base (criterion 3)", () => {
  it("issues GET /api/projects/{cid}/release on the agent origin, never the app server", async () => {
    const { getProjectRelease: liveGetProjectRelease } = await importLiveProjects();
    const wire: ReleaseResponse = {
      assessment: "candidate",
      reasons: [],
      blockers: [],
      required_env: [{ name: "PORT", scope: "runtime", required: true, secret: false }],
      command: "docker compose up -d --build",
      ingress: { service: "web", port: "PORT", health_path: null },
      self_host: true,
      spec_digest: "sha256:x",
      version_seq: 1,
      tree_digest: "sha256:y",
    };
    const stub = makeFetchStub(wire);
    vi.stubGlobal("fetch", stub);

    const res = await liveGetProjectRelease("conv_x");
    expect(res.assessment).toBe("candidate");

    const call = stub.mock.calls.find(([url]) => String(url).includes("/release"));
    expect(call).toBeDefined();
    const [url, init] = call as unknown as [string, RequestInit];
    expect(url).toBe("http://agent/api/projects/conv_x/release");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");

    // Every fetch went to the AGENT origin — nothing hit an app-server base.
    for (const [u] of stub.mock.calls) {
      expect(String(u).startsWith("http://agent")).toBe(true);
    }
  });

  it("percent-encodes the conversation id in the release path", async () => {
    const { getProjectRelease: liveGetProjectRelease } = await importLiveProjects();
    const stub = makeFetchStub({
      assessment: "not_web",
      reasons: [],
      blockers: [],
      required_env: [],
      command: "docker compose up -d --build",
      ingress: null,
      self_host: false,
      spec_digest: null,
      version_seq: 1,
      tree_digest: "sha256:z",
    });
    vi.stubGlobal("fetch", stub);

    await liveGetProjectRelease("conv/../x");

    const call = stub.mock.calls.find(([url]) => String(url).includes("/release"));
    expect(call).toBeDefined();
    const [url] = call as unknown as [string, RequestInit];
    expect(url).toBe("http://agent/api/projects/conv%2F..%2Fx/release");
  });
});
