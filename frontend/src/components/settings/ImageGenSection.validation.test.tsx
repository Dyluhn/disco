/**
 * ImageGenSection — workflow-validation regression (Firefox defect).
 *
 * Reproduces: choosing ComfyUI then entering `{invalid` shows an error but
 * Save still sends PUT 200 and the bad workflow persists after reload.
 *
 * Drives the REAL path: ImageGenSection → useImageGenConfig /
 * useUpdateImageGenConfig → @/api/models → fetch(...). Only the network
 * boundary is stubbed (vi.stubGlobal "fetch") + isLive intercepted, matching
 * ImageGenSection.test.tsx. NO mocks of component state/handlers.
 *
 * Escape hatch (must remain): the user can always switch to OpenAI-compatible
 * / OpenRouter despite a bad workflow draft; alternate providers and their
 * fields must NOT be disabled by validation.
 *
 * Dummy endpoint/key references only — no real network.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ImageGenSection } from "./ImageGenSection";

const { agentLiveMock } = vi.hoisted(() => ({ agentLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => true,
  agentLive: () => agentLiveMock(),
}));

// Credential-name selection is tested at its own boundary.
vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({ data: { names: [], locked_names: [] } }),
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

type PutRecord = { url: string; body: Record<string, unknown> };

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

async function openImageConfiguration() {
  const summary = await screen.findByText(/Configure image generation/i, {
    selector: "summary",
  });
  fireEvent.click(summary);
  expect(summary.closest("details")).toHaveAttribute("open");
}

function getSaveButton(): HTMLElement {
  return screen.getByRole("button", { name: /^Save$/ });
}

function getTestButton(): HTMLElement {
  return screen.getByRole("button", { name: /Test image/ });
}

/**
 * Stateful app-server fixture: GET reflects the last PUT (reload persistence).
 * All other fetch traffic is captured so probe calls can be asserted on their
 * actual network path without touching a real network.
 */
function installStatefulFixture(initial: Record<string, unknown>, opts?: { putDelayMs?: number }) {
  let saved = { ...initial };
  const puts: PutRecord[] = [];
  const probeCalls: { url: string; init?: RequestInit }[] = [];
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/image-gen/config") {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        if (opts?.putDelayMs) {
          await new Promise((r) => setTimeout(r, opts.putDelayMs));
        }
        puts.push({ url, body });
        saved = { ...body };
        return jsonResponse({ ...saved });
      }
      return jsonResponse({ ...saved });
    }
    if (typeof url === "string" && url.startsWith("/api/models/openrouter")) {
      return jsonResponse([]);
    }
    if (url === "/api/openrouter/key") {
      return jsonResponse({ configured: false });
    }
    if (url === "/api/image-gen/test" && init?.method === "POST") {
      probeCalls.push({ url, init });
      return jsonResponse({ ok: true, status: "ok", detail: "dummy probe ok" });
    }
    throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return { stub, puts, probeCalls, getSaved: () => ({ ...saved }) };
}

const COMFY_SAVED = {
  provider: "comfyui",
  base_url: "http://127.0.0.1:8188",
  api_key_env: "",
  model: "dummy-checkpoint.safetensors",
  workflow_json: "",
};

beforeEach(() => {
  agentLiveMock.mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ImageGenSection — malformed ComfyUI workflow validation", () => {
  it("malformed active draft shows an error, keeps Save disabled, and sends no PUT", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    const textarea = await screen.findByPlaceholderText(/Save \(API Format\)/i);
    fireEvent.change(textarea, { target: { value: "{invalid" } });

    // Validation error is displayed (wording intentionally loose).
    expect(await screen.findByText(/Not valid JSON yet/i)).toBeInTheDocument();

    const save = getSaveButton();
    expect(save).toBeDisabled();

    // Attempting Save must not reach the network.
    fireEvent.click(save);
    expect(fx.puts.length).toBe(0);
    expect(fx.getSaved().workflow_json).toBe("");
  });

  it("persisted malformed active workflow never shows Saved and keeps Test image disabled", async () => {
    installStatefulFixture({ ...COMFY_SAVED, workflow_json: "{invalid" });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    await screen.findByDisplayValue("{invalid");
    expect(screen.getByText(/Not valid JSON yet/i)).toBeInTheDocument();

    // No misleading Saved indicator while the saved value itself is broken.
    expect(screen.queryByText(/^Saved$/)).not.toBeInTheDocument();

    const test = getTestButton();
    expect(test).toBeDisabled();
  });

  it("provider picker stays usable with a bad draft; switching away PUTs and preserves drafts", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    const textarea = await screen.findByPlaceholderText(/Save \(API Format\)/i);
    fireEvent.change(textarea, { target: { value: "{invalid" } });
    expect(await screen.findByText(/Not valid JSON yet/i)).toBeInTheDocument();

    const paid = await screen.findByRole("button", { name: /Paid API/ });
    const router = await screen.findByRole("button", { name: /OpenRouter/ });
    const comfy = await screen.findByRole("button", { name: /Self-hosted \(ComfyUI\)/ });
    // Escape hatch: alternates must stay clickable despite the bad draft.
    expect(paid).toBeEnabled();
    expect(router).toBeEnabled();
    expect(comfy).toBeEnabled();

    fireEvent.click(paid);
    await waitFor(() => expect(fx.puts.length).toBe(1));
    expect(fx.puts[0].body.provider).toBe("openai");
    // Drafts travel with the switch so edits are not lost.
    expect(fx.puts[0].body.workflow_json).toBe("{invalid");

    // Switching back restores the preserved bad draft for further editing.
    fireEvent.click(await screen.findByRole("button", { name: /Self-hosted \(ComfyUI\)/ }));
    await waitFor(() => expect(fx.puts.length).toBe(2));
    await screen.findByDisplayValue("{invalid");
  });

  it("OpenAI-compatible with inactive malformed workflow still edits, saves, and probes", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    // Park a bad workflow draft, then escape to the paid tier.
    fireEvent.change(await screen.findByPlaceholderText(/Save \(API Format\)/i), {
      target: { value: "{invalid" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Paid API/ }));
    await waitFor(() => expect(fx.puts.length).toBe(1));
    fx.puts.length = 0;
    fx.probeCalls.length = 0;

    // Paid-tier fields stay editable and savable while the bad draft is inactive/hidden.
    const endpoint = await screen.findByPlaceholderText(/api\.openai\.com/i);
    fireEvent.change(endpoint, { target: { value: "https://dummy.example.invalid" } });
    const save = getSaveButton();
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(fx.puts.length).toBe(1));
    expect(fx.puts[0].body.provider).toBe("openai");

    // After save (no unsaved changes) the probe runs against the real probe
    // path captured at the fetch boundary — no real network.
    const test = getTestButton();
    await waitFor(() => expect(test).toBeEnabled());
    fx.probeCalls.length = 0;
    fireEvent.click(test);
    await waitFor(() => expect(fx.probeCalls.length).toBeGreaterThan(0));
    expect(fx.probeCalls).toEqual([{ url: "/api/image-gen/test", init: expect.objectContaining({ method: "POST" }) }]);
  });

  it("OpenRouter with inactive malformed workflow still edits, saves, and probes", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    fireEvent.change(await screen.findByPlaceholderText(/Save \(API Format\)/i), {
      target: { value: "{invalid" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /OpenRouter/ }));
    await waitFor(() => expect(fx.puts.length).toBe(1));
    fx.puts.length = 0;
    fx.probeCalls.length = 0;

    // Empty catalogue uses the real text fallback; editing must not be skipped.
    fireEvent.change(await screen.findByPlaceholderText("google/gemini-2.5-flash-image"), {
      target: { value: "dummy/image-model" },
    });
    const save = getSaveButton();
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(fx.puts.length).toBe(1));
    expect(fx.puts[0].body).toEqual(expect.objectContaining({
      provider: "openrouter", model: "dummy/image-model", workflow_json: "{invalid",
    }));

    const test = getTestButton();
    await waitFor(() => expect(test).toBeEnabled());
    fx.probeCalls.length = 0;
    fireEvent.click(test);
    await waitFor(() => expect(fx.probeCalls.length).toBeGreaterThan(0));
    expect(fx.probeCalls).toEqual([{ url: "/api/image-gen/test", init: expect.objectContaining({ method: "POST" }) }]);
  });

  it("clearing a bad workflow to empty restores the built-in-default save/probe path", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED, workflow_json: "{invalid" });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    const textarea = await screen.findByDisplayValue("{invalid");
    fireEvent.change(textarea, { target: { value: "" } });

    // Empty = built-in graph, not an error.
    expect(screen.queryByText(/valid JSON/i)).not.toBeInTheDocument();

    fireEvent.click(getSaveButton());
    await waitFor(() => expect(fx.puts.length).toBe(1));
    expect(fx.puts[0].body.workflow_json).toBe("");

    const test = getTestButton();
    await waitFor(() => expect(test).toBeEnabled());
    fx.probeCalls.length = 0;
    fireEvent.click(test);
    await waitFor(() => expect(fx.probeCalls.length).toBeGreaterThan(0));
  });

  it("valid templated graph with unquoted %seed% and quoted %prompt% saves exactly", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    const graph =
      '{ "3": { "class_type": "KSampler", "inputs": { "seed": %seed%, "prompt": "%prompt%" } } }';
    fireEvent.change(await screen.findByPlaceholderText(/Save \(API Format\)/i), {
      target: { value: graph },
    });
    expect(screen.queryByText(/valid JSON/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Numeric tokens .* must be UNQUOTED/i)).not.toBeInTheDocument();

    fireEvent.click(getSaveButton());
    await waitFor(() => expect(fx.puts.length).toBe(1));
    expect(fx.puts[0].body.workflow_json).toBe(graph);
  });

  it("unsaved and offline guards keep Test image honest", async () => {
    // Unsaved changes disable the probe with guidance.
    const fx = installStatefulFixture({ ...COMFY_SAVED });
    const view = render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();
    await screen.findByPlaceholderText(/Save \(API Format\)/i);

    // Clean state probes fine; dirty state must not.
    await waitFor(() => expect(getTestButton()).toBeEnabled());
    fireEvent.change(screen.getByDisplayValue("dummy-checkpoint.safetensors"), {
      target: { value: "dummy-checkpoint-v2.safetensors" },
    });
    const dirtyTest = getTestButton();
    expect(dirtyTest).toBeDisabled();
    expect(screen.getByText(/unsaved changes/i)).toBeInTheDocument();
    expect(fx.probeCalls.length).toBe(0);

    // Offline disables the probe even when saved.
    view.unmount();
    agentLiveMock.mockReturnValue(false);
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();
    const offlineTest = await screen.findByRole("button", { name: /Test image/ });
    expect(offlineTest).toBeDisabled();
    expect(await screen.findByText(/agent server offline/i)).toBeInTheDocument();
  });

  it("Save stays disabled while a save is pending", async () => {
    const fx = installStatefulFixture({ ...COMFY_SAVED }, { putDelayMs: 150 });
    render(createElement(ImageGenSection), { wrapper: makeWrapper() });
    await openImageConfiguration();

    fireEvent.change(await screen.findByPlaceholderText(/Save \(API Format\)/i), {
      target: {
        value: '{ "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "%ckpt%" } } }',
      },
    });
    const save = getSaveButton();
    expect(save).toBeEnabled();
    fireEvent.click(save);
    // Pending mutation disables Save (spinner state).
    await waitFor(() => expect(getSaveButton()).toBeDisabled());
    await waitFor(() => expect(fx.puts).toHaveLength(1));
    expect(fx.getSaved().workflow_json).toContain("CheckpointLoaderSimple");
  });
});
