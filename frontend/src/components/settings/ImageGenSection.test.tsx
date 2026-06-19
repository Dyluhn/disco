/**
 * ImageGenSection — A3. Drives the REAL data path: ImageGenSection →
 * useImageGenConfig / useUpdateImageGenConfig → @/api/models → fetch(...).
 * Only the network boundary is stubbed (vi.stubGlobal "fetch") + isLive spied,
 * matching DataSourcesSection.test.tsx / ProviderKeysSection.test.tsx. NO vi.mock
 * of the data layer.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { ImageGenSection } from "./ImageGenSection";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

let lastPut: { url: string; body: unknown } | null = null;

function installFetch() {
  lastPut = null;
  const stub = vi.fn(async (url: string, opts?: RequestInit) => {
    if (url === "/api/image-gen/config") {
      if (opts?.method === "PUT") {
        const body = JSON.parse(String(opts.body));
        lastPut = { url, body };
        return jsonResponse(body); // app-server echoes the saved config
      }
      return jsonResponse({ provider: "procedural", base_url: "", api_key_env: "" });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ImageGenSection — A3 image-gen provider settings", () => {
  it("renders the three tiers from the live config with procedural active", async () => {
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    const procedural = await screen.findByRole("button", { name: /Bundled \(procedural\)/ });
    expect(procedural).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Self-hosted \(ComfyUI\)/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Paid API/ })).toBeInTheDocument();
  });

  it("PUTs the chosen provider and shows the ComfyUI base-URL field (required, no false default)", async () => {
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    const comfy = await screen.findByRole("button", { name: /Self-hosted \(ComfyUI\)/ });
    fireEvent.click(comfy);
    await waitFor(() => expect(lastPut).not.toBeNull());
    expect((lastPut!.body as { provider: string }).provider).toBe("comfyui");
    // truthful affordance: the base URL is required (not "empty → default")
    expect(screen.getByPlaceholderText(/required for ComfyUI/i)).toBeInTheDocument();
  });
});
