import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProvidersSection } from "./ProvidersSection";

// Amendment A3: components/ and views/ may not import `@/api/client`. `vi.mock`
// intercepts by specifier and needs no static import, so this controls exactly
// the same `isLive` the previous `vi.spyOn(clientModule, …)` did.
const { isLiveMock } = vi.hoisted(() => ({ isLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => isLiveMock(),
}));

vi.mock("./OpenRouterSection", () => ({
  OpenRouterSection: () => <h4>OpenRouter</h4>,
}));

vi.mock("./ProviderKeysSection", () => ({
  ProviderKeysSection: () => null,
}));

const presets = [
  {
    id: "openrouter",
    label: "OpenRouter",
    base_url: "https://openrouter.ai/api/v1",
    kind: "openai-compat",
  },
  {
    id: "openai",
    label: "OpenAI",
    base_url: "https://api.openai.com/v1",
    kind: "openai-compat",
  },
  {
    id: "ollama",
    label: "Ollama (local)",
    base_url: "http://host.docker.internal:11434/v1",
    kind: "openai-compat",
    requires_base_url: true,
    requires_api_key: false,
  },
  {
    id: "custom-openai-compatible",
    label: "Custom (OpenAI-compatible)",
    base_url: "",
    kind: "openai-compat",
    requires_base_url: true,
  },
];

type Provider = {
  id: string;
  label: string;
  base_url: string;
  kind: string;
  secret_name: string;
  has_key: boolean;
  requires_api_key?: boolean;
};

let providers: Provider[];
let models: Array<{
  id: string;
  label: string;
  provider: string;
  price_in_per_m: number;
  price_out_per_m: number;
  pricing_mode: string;
  capabilities: string[];
  model_id: string;
  base_url: string;
  api_key_env: string;
  context_window: number;
}>;
let catalogueFails = false;
let createCatalogueOk = true;
let lastEnableBody: unknown = null;
let lastCreateBody: Record<string, unknown> | null = null;

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

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url === "/api/providers/presets") {
      return jsonResponse(presets);
    }
    if (method === "GET" && url === "/api/providers") {
      return jsonResponse(providers);
    }
    if (method === "GET" && url === "/api/models") {
      return jsonResponse(models);
    }
    if (method === "POST" && url === "/api/providers") {
      const body = JSON.parse(String(init?.body));
      lastCreateBody = body;
      const keyless = body.requires_api_key === false;
      const id = keyless ? "ollama" : "openai";
      const provider: Provider = {
        id,
        label: body.label,
        base_url: body.base_url,
        kind: body.kind,
        secret_name: `provider_${id}`,
        has_key: !!body.api_key,
        requires_api_key: !keyless,
      };
      providers = [provider];
      return jsonResponse(
        {
          provider,
          catalogue_ok: createCatalogueOk,
          catalogue_error: createCatalogueOk ? null : "404 from /models",
        },
        201,
      );
    }
    if (method === "GET" && url === "/api/providers/ollama/models") {
      return jsonResponse([]);
    }
    if (method === "GET" && url === "/api/providers/openai/models") {
      if (catalogueFails) {
        return jsonResponse({ detail: "OpenAI /models probe failed: 404 from /models" }, 502);
      }
      return jsonResponse([
        {
          model_id: "gpt-4o-mini",
          label: "GPT-4o mini",
          context_window: 128000,
          price_in_per_m: null,
          price_out_per_m: null,
          capabilities: ["long_context"],
        },
        {
          model_id: "gpt-4o",
          label: "GPT-4o",
          context_window: 128000,
          price_in_per_m: 2.5,
          price_out_per_m: 10,
          capabilities: ["vision", "long_context"],
        },
      ]);
    }
    if (method === "POST" && url === "/api/providers/openai/enable") {
      const body = JSON.parse(String(init?.body));
      lastEnableBody = body;
      const id = `prov-openai-${String(body.model_id).replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
      models = [
        ...models,
        {
          id,
          label: `Openai ${body.model_id}`,
          provider: "openrouter",
          price_in_per_m: body.model_id === "gpt-4o" ? 2.5 : 0,
          price_out_per_m: body.model_id === "gpt-4o" ? 10 : 0,
          pricing_mode: body.model_id === "gpt-4o" ? "metered" : "free",
          capabilities: [],
          model_id: body.model_id,
          base_url: "https://api.openai.com/v1",
          api_key_env: "provider_openai",
          context_window: 8192,
        },
      ];
      return jsonResponse(models);
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  providers = [];
  models = [];
  catalogueFails = false;
  createCatalogueOk = true;
  lastEnableBody = null;
  lastCreateBody = null;
  isLiveMock.mockReturnValue(true);
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ProvidersSection — generic provider objects", () => {
  it("offers OpenRouter only through its canonical dedicated surface", async () => {
    render(createElement(ProvidersSection), { wrapper: makeWrapper() });

    const preset = await screen.findByLabelText("Provider preset");
    expect(
      preset.querySelector('option[value="openrouter"]'),
    ).not.toBeInTheDocument();
    const openRouter = screen.getByText("OpenRouter", { selector: "summary" });
    expect(openRouter.closest("details")).not.toHaveAttribute("open");
    const advanced = screen.getByText("Advanced provider keys", { selector: "summary" });
    expect(advanced.closest("details")).toHaveAttribute("id", "advanced-provider-keys");
    expect(advanced.closest("details")).not.toHaveAttribute("open");
  });

  it("adds Ollama without inventing a key and exposes the Compose-safe URL", async () => {
    const user = userEvent.setup();
    render(createElement(ProvidersSection), { wrapper: makeWrapper() });

    await screen.findByRole("option", { name: /Ollama \(local\)/i });
    await user.selectOptions(screen.getByLabelText("Provider preset"), "ollama");

    await waitFor(() =>
      expect(screen.queryByLabelText("Provider API key")).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Custom provider base URL")).toHaveValue(
      "http://host.docker.internal:11434/v1",
    );
    expect(screen.getByRole("button", { name: /Add provider/i })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /Add provider/i }));

    await waitFor(() =>
      expect(lastCreateBody).toMatchObject({
        label: "Ollama (local)",
        base_url: "http://host.docker.internal:11434/v1",
        api_key: "",
        requires_api_key: false,
      }),
    );
    expect(await screen.findByText("No key needed")).toBeInTheDocument();
  });

  it("adds a provider, opens browse, and toggles a model into the catalogue", async () => {
    render(createElement(ProvidersSection), { wrapper: makeWrapper() });

    fireEvent.change(await screen.findByLabelText("Provider preset"), {
      target: { value: "openai" },
    });
    fireEvent.change(screen.getByLabelText("Provider API key"), {
      target: { value: "sk-live-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add provider/i }));

    expect(await screen.findByText("Key ready")).toBeInTheDocument();
    expect(await screen.findByLabelText("Search OpenAI models")).toBeInTheDocument();
    expect(screen.queryByText("sk-live-secret")).not.toBeInTheDocument();
    expect(await screen.findByText(/pricing unknown/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: /Enable gpt-4o-mini/i }));

    await waitFor(() =>
      expect(lastEnableBody).toEqual({
        model_id: "gpt-4o-mini",
        label: "GPT-4o mini",
        context_window: 128000,
      }),
    );
    await waitFor(() => expect(screen.getByText(/1 enabled/i)).toBeInTheDocument());
    expect(screen.queryByText("sk-live-secret")).not.toBeInTheDocument();
  });

  it("renders the failed-/models manual-add path and uses the same enable route", async () => {
    catalogueFails = true;
    createCatalogueOk = false;
    render(createElement(ProvidersSection), { wrapper: makeWrapper() });

    fireEvent.change(await screen.findByLabelText("Provider preset"), {
      target: { value: "openai" },
    });
    fireEvent.change(screen.getByLabelText("Provider API key"), {
      target: { value: "sk-bad-relay" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add provider/i }));

    expect(
      await screen.findByText(/was saved, but \/models did not answer/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/did not return a usable \/models catalogue/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Manual model ID for OpenAI"), {
      target: { value: "relay/model-1" },
    });
    // Context is REQUIRED for manual adds (no catalogue to trust) — the button
    // stays disabled until a plausible window is entered; nothing defaults.
    const add = screen.getByRole("button", { name: /Add model ID/i });
    expect(add).toBeDisabled();
    fireEvent.change(
      screen.getByLabelText("Context window for manual model on OpenAI"),
      { target: { value: "131072" } },
    );
    expect(add).toBeEnabled();
    fireEvent.click(add);
    // An opaque manually-added model must answer the one required modality
    // question before the enable request is sent.
    fireEvent.click(await screen.findByRole("button", { name: "Supports images" }));

    await waitFor(() =>
      expect(lastEnableBody).toEqual({
        model_id: "relay/model-1",
        label: "relay/model-1",
        context_window: 131072,
        max_output_tokens: null,
        vision: true,
      }),
    );
    expect(screen.queryByText("sk-bad-relay")).not.toBeInTheDocument();
  });
});
