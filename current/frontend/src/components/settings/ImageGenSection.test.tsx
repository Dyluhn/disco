/**
 * ImageGenSection — A3. Drives the REAL data path: ImageGenSection →
 * useImageGenConfig / useUpdateImageGenConfig → @/api/models → fetch(...).
 * Only the network boundary is stubbed (vi.stubGlobal "fetch") + isLive
 * intercepted, matching DataSourcesSection.test.tsx / ProviderKeysSection.test.tsx.
 * NO vi.mock of the data layer.
 *
 * Amendment A3: components/ and views/ may not import `@/api/client`. `vi.mock`
 * intercepts by specifier and needs no static import, so this controls exactly
 * the same `isLive` the previous `vi.spyOn(clientModule, …)` did.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ImageGenSection } from "./ImageGenSection";

const { isLiveMock } = vi.hoisted(() => ({ isLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => isLiveMock(),
}));

// Credential-name selection is tested at its own boundary. Keep this suite's
// network contract focused on image configuration rather than `/api/secrets`.
vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({ data: { names: [], locked_names: [] } }),
  useSetSecret: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

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
      // W-50: no bundled tier — default the test config to a real (paid) tier.
      return jsonResponse({ provider: "openai", base_url: "", api_key_env: "", model: "" });
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

async function openImageConfiguration() {
  const summary = await screen.findByText(/Configure image generation/i, {
    selector: "summary",
  });
  expect(summary.closest("details")).not.toHaveAttribute("open");
  fireEvent.click(summary);
  expect(summary.closest("details")).toHaveAttribute("open");
}

beforeEach(() => {
  isLiveMock.mockReturnValue(true);
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ImageGenSection — A3 image-gen provider settings", () => {
  it("renders the three real tiers from the live config with the configured tier active", async () => {
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    expect(await screen.findByText(/Current: Paid API/i)).toBeInTheDocument();
    await openImageConfiguration();
    // W-50: there is NO bundled "Bundled (procedural)" tier anymore.
    const paid = await screen.findByRole("button", { name: /Paid API/ });
    expect(paid).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Self-hosted \(ComfyUI\)/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /OpenRouter/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Bundled \(procedural\)/ })).not.toBeInTheDocument();
  });

  it("PUTs the chosen provider and shows the ComfyUI base-URL field (required, no false default)", async () => {
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();
    const comfy = await screen.findByRole("button", { name: /Self-hosted \(ComfyUI\)/ });
    fireEvent.click(comfy);
    await waitFor(() => expect(lastPut).not.toBeNull());
    expect((lastPut!.body as { provider: string }).provider).toBe("comfyui");
    // truthful affordance: the base URL is required (not "empty → default")
    expect(screen.getByPlaceholderText(/required for ComfyUI/i)).toBeInTheDocument();
  });

  it("shows the ComfyUI custom-workflow textarea and PUTs workflow_json on save", async () => {
    // Start with comfyui already active (base_url set) so the contextual fields render.
    vi.unstubAllGlobals();
    lastPut = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, opts?: RequestInit) => {
        if (url !== "/api/image-gen/config") throw new Error(`unexpected fetch: ${url}`);
        if (opts?.method === "PUT") {
          const body = JSON.parse(String(opts.body));
          lastPut = { url, body };
          return jsonResponse(body);
        }
        return jsonResponse({
          provider: "comfyui",
          base_url: "http://host:8188",
          api_key_env: "",
          model: "sd_xl.safetensors",
          workflow_json: "",
        });
      }),
    );
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    const textarea = await screen.findByPlaceholderText(/Save \(API Format\)/i);
    const graph = '{ "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "%ckpt%" } } }';
    fireEvent.change(textarea, { target: { value: graph } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));

    await waitFor(() => expect(lastPut).not.toBeNull());
    const body = lastPut!.body as { provider: string; workflow_json: string };
    expect(body.provider).toBe("comfyui");
    expect(body.workflow_json).toBe(graph);
  });

  it("warns that a remote tier selected without its required config is NOT configured (W-50)", async () => {
    // openai persisted but no api_key_env → image-gen is NOT configured (no procedural
    // fallback); the UI must say so rather than imply a backend is active.
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/image-gen/config"
          ? jsonResponse({ provider: "openai", base_url: "", api_key_env: "", model: "" })
          : (() => {
              throw new Error(`unexpected fetch: ${url}`);
            })(),
      ),
    );
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    // The warning carries the distinct "until then" phrasing; the test button may
    // also render a status when the agent server is offline.
    const warning = await screen.findByText(/until then.*not configured/i);
    expect(warning).toHaveTextContent(/until then.*not configured/i);
  });
});

describe("ImageGenSection — OpenRouter priced image-model picker", () => {
  it("shows a priced picker of image-output models and PUTs the chosen model", async () => {
    isLiveMock.mockReturnValue(true);
    let lastPutLocal: { url: string; body: unknown } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, opts?: RequestInit) => {
        if (url === "/api/image-gen/config") {
          if (opts?.method === "PUT") {
            const body = JSON.parse(String(opts.body));
            lastPutLocal = { url, body };
            return jsonResponse(body);
          }
          return jsonResponse({ provider: "openrouter", base_url: "", api_key_env: "", model: "" });
        }
        if (url.startsWith("/api/models/openrouter")) {
          return jsonResponse([
            { id: "some/text-llm", name: "Text", context_length: 128000, price_in_per_m: 1, price_out_per_m: 2, capabilities: ["tool_calling"] },
            { id: "google/gemini-2.5-flash-image", name: "Gemini Flash Image", context_length: 32768, price_in_per_m: 0.3, price_out_per_m: 2.5, capabilities: ["vision"], image_output: true, image_price_per_m: 30 },
            { id: "black-forest-labs/flux.2-flex", name: "FLUX.2 Flex", context_length: 0, price_in_per_m: 0, price_out_per_m: 0, capabilities: [], image_output: true, image_price_per_m: 14.65 },
          ]);
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    // the priced picker appears (a <select>) and lists ONLY the image-output models,
    // each showing the real per-image-token cost ($X /M img-tok) enriched from /endpoints.
    const picker = await screen.findByRole("combobox");
    const opts = Array.from(picker.querySelectorAll("option")).map((o) => o.textContent);
    expect(opts.some((t) => /gemini-2\.5-flash-image.*\$30\.00 \/M img-tok/.test(t ?? ""))).toBe(true);
    expect(opts.some((t) => /text-llm/.test(t ?? ""))).toBe(false); // text model filtered out
    // The dedicated per-image generator (FLUX, catalogue token-price 0/0) shows its real
    // enriched image price — NOT "Free" and NOT the "pricing on openrouter.ai" fallback.
    const flux = opts.find((t) => /flux\.2-flex/.test(t ?? "")) ?? "";
    expect(flux).toMatch(/\$14\.65 \/M img-tok/);
    expect(flux).not.toMatch(/Free|pricing on openrouter/);

    fireEvent.change(picker, { target: { value: "google/gemini-2.5-flash-image" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(lastPutLocal).not.toBeNull());
    expect((lastPutLocal!.body as { model: string }).model).toBe("google/gemini-2.5-flash-image");
  });
});
