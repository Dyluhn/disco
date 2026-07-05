import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { importProject } from "@/api/projects";
import * as clientModule from "@/api/client";

function makeFetchStub(body: object, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

describe("importProject", () => {
  beforeEach(() => {
    vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
    vi.spyOn(clientModule, "agentHttpBase").mockReturnValue("http://agent");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("posts a zip as multipart form data", async () => {
    const payload = { conversation_id: "conv_zip", files: 1, bytes: 5, title: "App" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    const file = new File(["hello"], "app.zip", { type: "application/zip" });
    const result = await importProject({ kind: "zip", file });

    expect(result).toEqual(payload);
    const [url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://agent/api/projects/import?owner_id=local");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string> | undefined)?.["content-type"]).toBeUndefined();
    expect((init.body as FormData).get("file")).toBe(file);
  });

  it("posts a local folder path as JSON", async () => {
    const payload = { conversation_id: "conv_path", files: 2, bytes: 10, title: "Existing" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    await importProject({ kind: "path", path: "/tmp/existing" });

    const [_url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toEqual({ "content-type": "application/json" });
    expect(init.body).toBe(JSON.stringify({ path: "/tmp/existing" }));
  });

  it("posts a git URL as JSON", async () => {
    const payload = { conversation_id: "conv_git", files: 3, bytes: 20, title: "Repo" };
    const stub = makeFetchStub(payload);
    vi.stubGlobal("fetch", stub);

    await importProject({ kind: "git", gitUrl: "https://example.com/repo.git" });

    const [_url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(JSON.stringify({ git_url: "https://example.com/repo.git" }));
  });

  it("surfaces the backend import error reason and message", async () => {
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
