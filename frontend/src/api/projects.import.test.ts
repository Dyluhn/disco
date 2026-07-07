import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { importProject as importProjectFn } from "@/api/projects";

type ImportProject = typeof importProjectFn;

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

async function importLiveProjects(): Promise<{ importProject: ImportProject }> {
  vi.resetModules();
  vi.stubGlobal("__DISCO_ENV", { AGENT_BASE: "http://agent" });
  return import("@/api/projects");
}

function importCall(stub: ReturnType<typeof vi.fn>) {
  const call = stub.mock.calls.find(([url]) =>
    String(url).includes("/api/projects/import"),
  );
  if (!call) throw new Error("project import endpoint was not called");
  return call as [string, RequestInit];
}

describe("importProject", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts a zip as multipart form data", async () => {
    const { importProject } = await importLiveProjects();
    const payload = { conversation_id: "conv_zip", files: 1, bytes: 5, title: "App" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    const file = new File(["hello"], "app.zip", { type: "application/zip" });
    const result = await importProject({ kind: "zip", file });

    expect(result).toEqual(payload);
    const [url, init] = importCall(stub);
    expect(url).toBe("http://agent/api/projects/import");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("x-disco-csrf")).toBe("csrf-token");
    expect(init.body).toBeInstanceOf(FormData);
    expect(new Headers(init.headers).get("content-type")).toBeNull();
    expect((init.body as FormData).get("file")).toBe(file);
  });

  it("posts a local folder path as JSON", async () => {
    const { importProject } = await importLiveProjects();
    const payload = { conversation_id: "conv_path", files: 2, bytes: 10, title: "Existing" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    await importProject({ kind: "path", path: "/tmp/existing" });

    const [_url, init] = importCall(stub);
    expect(new Headers(init.headers).get("content-type")).toBe("application/json");
    expect(new Headers(init.headers).get("x-disco-csrf")).toBe("csrf-token");
    expect(init.body).toBe(JSON.stringify({ path: "/tmp/existing" }));
  });

  it("posts a git URL as JSON", async () => {
    const { importProject } = await importLiveProjects();
    const payload = { conversation_id: "conv_git", files: 3, bytes: 20, title: "Repo" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    await importProject({ kind: "git", gitUrl: "https://example.com/repo.git" });

    const [_url, init] = importCall(stub);
    expect(init.body).toBe(JSON.stringify({ git_url: "https://example.com/repo.git" }));
  });

  it("surfaces the backend import error reason and message", async () => {
    const { importProject } = await importLiveProjects();
    vi.stubGlobal(
      "fetch",
      makeFetchStub(
        { detail: { reason: "zip_slip", message: "zip entry escapes the project root" } },
        400,
      ),
    );

    await expect(
      importProject({ kind: "zip", file: new File(["x"], "bad.zip") }),
    ).rejects.toThrow("zip_slip: zip entry escapes the project root");
  });
});
