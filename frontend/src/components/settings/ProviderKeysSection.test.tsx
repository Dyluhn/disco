/**
 * ProviderKeysSection live test — encrypt-all-keys S4.
 *
 * Drives the REAL data path: ProviderKeysSection → useSecrets/useSetSecret →
 * @/api/secrets → @/api/client → fetch(...). Only the network boundary is
 * stubbed (vi.stubGlobal("fetch")), same seam as ProjectStorageSection.test.tsx.
 * NO vi.mock of the data layer (that would let broken wiring pass).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { ProviderKeysSection } from "./ProviderKeysSection";

let names: string[];
let lockedFlag = false;
// provider config the cross-reference reads (api_key_env names the app expects)
let modelKeyEnvs: string[] = [];
let searchKeyEnv = "";
let extractionKeyEnv = "";
let ttsKeyEnv = "";

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
    if (method === "GET" && url === "/api/secrets") {
      // when locked, the whole box can't decrypt → every stored name is locked
      const locked_names = lockedFlag ? [...names].sort() : [];
      return jsonResponse({
        names: [...names].sort(),
        locked_names,
        locked: lockedFlag,
        can_store: true,
      });
    }
    // the cross-reference reads the current provider config:
    if (method === "GET" && url === "/api/models") {
      return jsonResponse(modelKeyEnvs.map((e) => ({ id: e, api_key_env: e })));
    }
    if (method === "GET" && url === "/api/data-sources/config") {
      return jsonResponse({
        search_api_key_env: searchKeyEnv,
        extraction_api_key_env: extractionKeyEnv,
      });
    }
    if (method === "GET" && url === "/api/tts/config") {
      return jsonResponse({ api_key_env: ttsKeyEnv });
    }
    if (method === "GET" && url === "/api/image-gen/config") {
      // useExpectedKeyNames() now also reads image-gen; procedural carries no key.
      return jsonResponse({ provider: "procedural", base_url: "", api_key_env: "", model: "" });
    }
    if (method === "POST" && url.endsWith("/test") && url.startsWith("/api/secrets/")) {
      // T4.1 probe: a real authenticated call would happen server-side; here the
      // boundary returns the honest result the section must render.
      return jsonResponse({ ok: true, status: "ok", detail: "the key works" });
    }
    if (method === "PUT" && url.startsWith("/api/secrets/")) {
      const name = decodeURIComponent(url.split("/api/secrets/")[1]);
      names.push(name);
      return jsonResponse({ name, configured: true, locked: false, can_store: true });
    }
    if (method === "DELETE" && url.startsWith("/api/secrets/")) {
      const name = decodeURIComponent(url.split("/api/secrets/")[1]);
      names = names.filter((n) => n !== name);
      return jsonResponse({ name, configured: false, locked: false, can_store: true });
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

let fetchStub: ReturnType<typeof vi.fn>;

beforeEach(() => {
  names = [];
  lockedFlag = false;
  modelKeyEnvs = [];
  searchKeyEnv = "";
  extractionKeyEnv = "";
  ttsKeyEnv = "";
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  fetchStub = installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ProviderKeysSection — store any provider key encrypted by name", () => {
  it("PUTs a new key by name and shows it in the stored list", async () => {
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    await screen.findByText("Provider API keys");

    fireEvent.change(screen.getByLabelText("Provider key env-var name"), {
      target: { value: "OPENAI_API_KEY" },
    });
    fireEvent.change(screen.getByLabelText("Provider key value"), {
      target: { value: "sk-openai-SECRET" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add key/i }));

    // the PUT fired against the real fetch path with the name in the URL...
    await waitFor(() =>
      expect(
        fetchStub.mock.calls.some(
          ([u, i]) =>
            (i?.method ?? "GET").toUpperCase() === "PUT" && u === "/api/secrets/OPENAI_API_KEY",
        ),
      ).toBe(true),
    );
    // ...the value rode in the body, NOT the URL (write-only key)...
    const putCall = fetchStub.mock.calls.find(
      ([, i]) => (i?.method ?? "GET").toUpperCase() === "PUT",
    );
    expect(String(putCall?.[1]?.body)).toContain("sk-openai-SECRET");
    // ...and the stored name now renders in the list.
    expect(await screen.findByText("OPENAI_API_KEY")).toBeInTheDocument();
  });

  it("rejects a non-env-var name inline without hitting the network", async () => {
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    await screen.findByText("Provider API keys");

    fireEvent.change(screen.getByLabelText("Provider key env-var name"), {
      target: { value: "bad name!" },
    });
    fireEvent.change(screen.getByLabelText("Provider key value"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /add key/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/env-var name/i);
    expect(
      fetchStub.mock.calls.some(([, i]) => (i?.method ?? "GET").toUpperCase() === "PUT"),
    ).toBe(false);
  });

  it("clears a stored key via DELETE", async () => {
    names = ["TAVILY_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    expect(await screen.findByText("TAVILY_API_KEY")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    await waitFor(() =>
      expect(
        fetchStub.mock.calls.some(
          ([u, i]) =>
            (i?.method ?? "GET").toUpperCase() === "DELETE" &&
            u === "/api/secrets/TAVILY_API_KEY",
        ),
      ).toBe(true),
    );
  });

  it("edits a stored key in place (PUT with the new value)", async () => {
    names = ["OPENAI_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    expect(await screen.findByText("OPENAI_API_KEY")).toBeInTheDocument();

    // reveal the inline edit input, type a new value, save
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    fireEvent.change(screen.getByLabelText("New value for OPENAI_API_KEY"), {
      target: { value: "sk-openai-ROTATED" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      const put = fetchStub.mock.calls.find(
        ([u, i]) =>
          (i?.method ?? "GET").toUpperCase() === "PUT" && u === "/api/secrets/OPENAI_API_KEY",
      );
      expect(put).toBeTruthy();
      expect(String(put?.[1]?.body)).toContain("sk-openai-ROTATED");
    });
  });

  it("names the SPECIFIC keys that can't be decrypted (not 'one or more')", async () => {
    lockedFlag = true;
    names = ["OPENAI_API_KEY", "TAVILY_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/can't be decrypted/i);
    // the alert names BOTH locked keys + the app secret
    expect(alert).toHaveTextContent("OPENAI_API_KEY");
    expect(alert).toHaveTextContent("TAVILY_API_KEY");
    expect(alert).toHaveTextContent(/DISCO_SECRET_KEY/);
    // never the vague "one or more"
    expect(alert).not.toHaveTextContent(/one or more/i);
    // each locked row is marked + offers a "Re-enter" affordance
    expect(screen.getAllByText("can't decrypt").length).toBe(2);
    expect(screen.getAllByRole("button", { name: /re-enter/i }).length).toBe(2);
  });

  it("flags a provider-referenced key that isn't stored, and prefills it", async () => {
    // a configured paid model references ANTHROPIC_API_KEY; a search provider
    // references TAVILY_API_KEY which IS stored.
    modelKeyEnvs = ["ANTHROPIC_API_KEY"];
    searchKeyEnv = "TAVILY_API_KEY";
    names = ["TAVILY_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });

    expect(await screen.findByText("Referenced by your providers")).toBeInTheDocument();
    // the stored one shows "stored"; the missing one offers an Add-key prefill
    expect(await screen.findByText("ANTHROPIC_API_KEY")).toBeInTheDocument();
    const addButtons = await screen.findAllByRole("button", { name: /add key/i });
    // there is the form's submit "Add key" PLUS the prefill "Add key" for the missing one
    expect(addButtons.length).toBeGreaterThanOrEqual(2);

    // clicking the missing key's prefill puts its name in the env-var input
    const prefill = addButtons.find((b) => b.tagName === "BUTTON" && b.textContent === "Add key" && b.className.includes("text-accent"));
    fireEvent.click(prefill ?? addButtons[0]);
    const nameInput = screen.getByLabelText("Provider key env-var name") as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("ANTHROPIC_API_KEY"));
  });

  it("T4.1: 'Test key' POSTs to the probe endpoint and renders the live result", async () => {
    names = ["OPENAI_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    expect(await screen.findByText("OPENAI_API_KEY")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /test key/i }));

    // it hit the REAL probe endpoint for that key...
    await waitFor(() =>
      expect(
        fetchStub.mock.calls.some(
          ([u, i]) =>
            (i?.method ?? "GET").toUpperCase() === "POST" &&
            u === "/api/secrets/OPENAI_API_KEY/test",
        ),
      ).toBe(true),
    );
    // ...and rendered the honest result chip.
    await waitFor(() =>
      expect(document.querySelector("[data-probe-status='ok']")).toBeInTheDocument(),
    );
    expect(screen.getByText(/the key works/)).toBeInTheDocument();
  });

  it("excludes the reserved OpenRouter env from the cross-reference", async () => {
    modelKeyEnvs = ["DISCO_OPENROUTER_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    await screen.findByText("Provider API keys");
    // OpenRouter has its own section; it must NOT appear as a generic expected key
    expect(screen.queryByText("Referenced by your providers")).not.toBeInTheDocument();
  });
});
