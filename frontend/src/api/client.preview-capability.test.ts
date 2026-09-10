import { afterEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body: object, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

async function importLiveClient() {
  vi.resetModules();
  vi.stubGlobal("__DISCO_ENV", { AGENT_BASE: "http://agent" });
  return import("@/api/client");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("preview capability ownership", () => {
  it("mints a distinct one-use launch for every concurrent consumer", async () => {
    const client = await importLiveClient();
    let mintNumber = 0;
    const fetchStub = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === "http://agent/api/auth/session") {
        return jsonResponse({ authenticated: true, csrf_token: "csrf" });
      }
      mintNumber += 1;
      const request = JSON.parse(String(init?.body)) as {
        transport: "host" | "path" | "path_live" | "canonical";
      };
      return jsonResponse({
        bootstrap_url: `http://${request.transport}.example/__disco/preview-auth`,
        bootstrap_intent: `${request.transport}-intent-${mintNumber}`,
      });
    });
    vi.stubGlobal("fetch", fetchStub);

    const launches = await Promise.all([
      client.previewBootstrapUrl("conv_deadbeef", 8000, "/?r=0"),
      client.previewBootstrapUrl("conv_deadbeef", 8000, "/?r=0"),
      client.pathPreviewBootstrapUrl("conv_deadbeef", "/?r=0"),
      client.pathPreviewBootstrapUrl("conv_deadbeef", "/?r=0"),
      client.livePathPreviewBootstrapUrl("conv_deadbeef", "/?r=0"),
      client.canonicalPreviewBootstrapUrl("conv_deadbeef", "/nested?mode=edit", 7),
    ]);

    expect(new Set(launches.map((launch) => launch?.intent)).size).toBe(6);
    const capabilityCalls = fetchStub.mock.calls.filter(([url]) =>
      String(url).includes("/preview/capability"),
    );
    expect(capabilityCalls).toHaveLength(6);
    expect(
      capabilityCalls.map(([, init]) => JSON.parse(String((init as RequestInit).body))),
    ).toEqual([
      { port: 8000, target_path: "/?r=0", transport: "host" },
      { port: 8000, target_path: "/?r=0", transport: "host" },
      { target_path: "/?r=0", transport: "path" },
      { target_path: "/?r=0", transport: "path" },
      { target_path: "/?r=0", transport: "path_live" },
      {
        workspace_version: 7,
        target_path: "/nested?mode=edit",
        transport: "canonical",
      },
    ]);
  });

  it("returns a canonical bare bootstrap with a body-only intent", async () => {
    const client = await importLiveClient();
    const fetchStub = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url) === "http://agent/api/auth/session") {
        return jsonResponse({ authenticated: true, csrf_token: "csrf" });
      }
      return jsonResponse({
        bootstrap_url: "http://path.example/__disco/path-preview-auth/deadbeef",
        bootstrap_intent: "signed-body-only-intent",
      });
    });
    vi.stubGlobal("fetch", fetchStub);

    const launch = await client.canonicalPreviewBootstrapUrl("conv_deadbeef", "/");
    expect(launch).toEqual({
      url: "http://path.example/__disco/path-preview-auth/deadbeef",
      intent: "signed-body-only-intent",
    });
    expect(launch?.url).not.toContain("signed-body-only-intent");
    expect(new URL(launch!.url).search + new URL(launch!.url).hash).toBe("");
  });
});
